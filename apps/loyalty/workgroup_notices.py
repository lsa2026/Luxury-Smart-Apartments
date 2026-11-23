"""Source-bound loyalty copies for the existing manual staff workgroup transport."""

from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from apps.accounts.models import CustomerProfile, VerifiedPhone
from apps.accounts.phone_messages import phone_logo, supported_language
from apps.reservations import telegram_workgroup as group
from apps.reservations.telegram_authority import Denied

from .launch import participant_allowed
from .models import GuestEvent, Member, MovementNotice

VERSION = "workgroup-loyalty-v1"
PURPOSES = {
    GuestEvent.Kind.JOIN: "loyalty_join",
    GuestEvent.Kind.CLOSE: "loyalty_close",
    GuestEvent.Kind.READY: "loyalty_ready",
}
WAITING = {"queued", "handoff_wait", "handoff_hold"}


def admitted(moment):
    try:
        cutoff = parse_datetime(getattr(settings, "LOYALTY_WORKGROUP_CUTOVER_AT", "") or "")
    except (TypeError, ValueError):
        return False
    return bool(
        cutoff
        and timezone.is_aware(cutoff)
        and timezone.is_aware(moment)
        and cutoff <= moment <= timezone.now()
    )


def enabled():
    return bool(settings.LOYALTY_ENABLED and group.manual_mode())


def queue(source, *, lifecycle=False):
    if not enabled() or not admitted(source.occurred_at):
        return None
    if lifecycle and (
        source.source == "history"
        or source.kind not in PURPOSES
        or not source.member_id
        or source.ledger_id
    ):
        return None
    customer = source.member.customer
    if not customer.is_active or not participant_allowed(customer):
        return None
    profile = CustomerProfile.objects.filter(user=customer).first()
    language = supported_language(profile.preferred_language if profile else "")
    row, created = MovementNotice.objects.get_or_create(
        **{"event" if lifecycle else "ledger": source},
        defaults={
            "language": language or supported_language(source.member.language) or "ar",
            "template_version": VERSION,
            "status": "queued",
        },
    )
    if created:
        from .whatsapp_notices import _dispatch

        transaction.on_commit(lambda: _dispatch(row.pk), robust=True)
    return row


@sensitive_variables()
def source_notice(source_id):
    from . import whatsapp_notices as notices

    row = MovementNotice.objects.select_related(
        "event__member__customer", "ledger__member__customer"
    ).get(pk=source_id)
    source = row.ledger if row.ledger_id else row.event
    member = source.member
    if (
        not enabled()
        or row.template_version != VERSION
        or row.status not in WAITING
        or not admitted(source.occurred_at)
        or not member.customer.is_active
        or not participant_allowed(member.customer)
    ):
        raise Denied
    if row.event_id and (
        source.source == "history"
        or source.kind not in PURPOSES
        or source.ledger_id
        or (source.kind != GuestEvent.Kind.CLOSE and member.status != "active")
    ):
        raise Denied
    if row.event_id and source.kind == GuestEvent.Kind.READY:
        from .services import invitation_eligible

        if not invitation_eligible(member):
            raise Denied
    profile = CustomerProfile.objects.filter(user=member.customer).first()
    binding = VerifiedPhone.objects.filter(user=member.customer).first()
    # An unverified contact is a forwarding destination, never ownership evidence.
    phone = binding.phone if binding else profile.phone if profile else ""
    body = (
        notices.movement_caption(source, row.language)
        if row.ledger_id
        else notices.lifecycle_caption(source, row.language)
    )
    if row.event_id and source.kind == GuestEvent.Kind.JOIN:
        from .welcome import image

        content = image(row.language)
    else:
        content = phone_logo()
    purpose = "loyalty_movement" if row.ledger_id else PURPOSES[source.kind]
    name = " ".join(member.customer.get_full_name().split())[:100]
    basis = {
        "notice": str(row.pk),
        "source": str(source.pk),
        "member": member.pk,
        "customer": member.customer_id,
        "member_status": member.status,
        "template": row.template_version,
        "purpose": purpose,
        "language": row.language,
        "occurred_at": source.occurred_at.isoformat(),
        "binding": str(binding.version) if binding else "",
        "phone_verified": bool(binding),
    }
    return body, content, row.language, phone, name, basis, source.occurred_at, purpose


def finish_notice(source_id, provider_id):
    MovementNotice.objects.filter(
        pk=source_id, template_version=VERSION, status__in=WAITING
    ).update(
        status="handed_off",
        accepted_at=timezone.now(),
        provider_reference=str(provider_id)[:80],
        error_code="workgroup_manual_copy",
    )


def deliver_notice(key):
    from apps.reservations.telegram_website_workgroup import deliver

    if not enabled():
        return
    with transaction.atomic():
        row = MovementNotice.objects.select_for_update().filter(pk=key).first()
        if not row or row.status != "queued":
            return
        if row.template_version != VERSION:
            row.status, row.error_code = "skipped", "legacy_transport_retired"
            row.save(update_fields=["status", "error_code"])
            return
        if not group.enabled():
            return
        try:
            source_notice(row.pk)
        except (Denied, ValueError, ValidationError):
            row.status, row.error_code = "skipped", "current_source_unavailable"
            row.save(update_fields=["status", "error_code"])
            return
        row.status, row.started_at = "handoff_wait", timezone.now()
        row.attempts += 1
        row.save(update_fields=["status", "started_at", "attempts"])
    try:
        result = deliver("loyalty_notice", str(key))
    except Exception:
        result = "uncertain"
    if result != "handed_off":
        MovementNotice.objects.filter(pk=key, status="handoff_wait").update(
            status="handoff_hold", error_code="workgroup_copy_unconfirmed"
        )


def recover(limit=100):
    from .whatsapp_notices import _dispatch

    count = 0
    # Only pending outbox rows. Legacy rows cannot become new workgroup events.
    for key in (
        MovementNotice.objects.filter(status="queued")
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[: min(max(limit, 1), 500)]
    ):
        _dispatch(key)
        count += 1
    return {"status": "workgroup_manual", "queued": count}


@sensitive_variables()
def source_gift(source_id):
    from . import phone_gifts as gifts
    from .invitation_links import invitation_path
    from .services import invitation_eligible

    member_id, key = source_id.split(":", 1)
    sender = Member.objects.select_related("customer").get(pk=member_id)
    row = sender.command_receipts.get(key, {}).get("result", {})
    created = parse_datetime(row.get("requested_at", ""))
    if (
        not enabled()
        or not key.startswith(gifts.PREFIX)
        or len(key) > 100
        or row.get("dispatch_mode") != "workgroup_manual"
        or row.get("status") not in WAITING
        or row.get("consent") is not True
        or row.get("template_version") != gifts.TEMPLATE_VERSION
        or not created
        or not admitted(created)
        or sender.status != "active"
        or not sender.invitation_token
        or not gifts._enabled(sender.customer)
        or not invitation_eligible(sender)
        or gifts.recipient_for_phone(row.get("phone"))[0]
    ):
        raise Denied
    base = settings.SITE_BASE_URL.rstrip("/")
    if urlparse(base).scheme != "https" or not urlparse(base).netloc:
        raise Denied
    language = row["language"]
    body = gifts.invitation_message(sender, language=language, url=base + invitation_path(sender))
    basis = {
        "member": sender.pk,
        "request": key,
        "invitation_token_digest": gifts.phone_hash(sender.invitation_token),
        "phone_digest": gifts.phone_hash(row["phone"]),
        "template": row["template_version"],
        "requested_at": row["requested_at"],
        "language": language,
    }
    return (
        body,
        gifts.invitation_image(language),
        language,
        row["phone"],
        gifts._sender_name(sender, language),
        basis,
        created,
        "loyalty_gift_invitation",
    )


def finish_gift(source_id, provider_id):
    from .phone_gifts import _finish

    member_id, key = source_id.split(":", 1)
    for expected in ("handoff_wait", "handoff_hold"):
        _finish(
            member_id, key, "handed_off", expected=expected, provider_reference=str(provider_id)
        )


def deliver_gift(member_id, key):
    from apps.reservations.telegram_website_workgroup import deliver

    from .phone_gifts import _finish
    from .services import posting_transaction

    with posting_transaction():
        sender = Member.objects.select_for_update().get(pk=member_id)
        row = sender.command_receipts.get(key, {}).get("result", {})
        if row.get("status") != "queued":
            return
        if row.get("dispatch_mode") != "workgroup_manual":
            row["status"] = "disabled"
            sender.save(update_fields=["command_receipts", "updated_at"])
            return
        if not group.enabled():
            return
        source_id = f"{member_id}:{key}"
        try:
            source_gift(source_id)
        except (Denied, ValueError, ValidationError):
            row["status"] = "disabled"
            sender.save(update_fields=["command_receipts", "updated_at"])
            return
        row["status"] = "handoff_wait"
        sender.save(update_fields=["command_receipts", "updated_at"])
    try:
        state = deliver("loyalty_gift", source_id)
    except Exception:
        state = "uncertain"
    if state != "handed_off":
        _finish(member_id, key, "handoff_hold", expected="handoff_wait")
