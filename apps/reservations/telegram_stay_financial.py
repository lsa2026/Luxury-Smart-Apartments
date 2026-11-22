"""Existing financial guards and approved copy, with a separate Telegram release proof."""

import hashlib
import re
from datetime import timedelta
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from . import message_source_authorization as source
from . import telegram_stay_render as render
from .day_use_stays import DayUseStayError
from .morning_stays import MorningStayError
from .telegram_authority import Denied
from .telegram_stay_models import TelegramStayMessage


def modules(scenario):
    if scenario == "07":
        from . import morning_loyalty as loyalty
        from . import morning_payment_messages as links
        from . import morning_stays as domain
    else:
        from . import day_use_loyalty as loyalty
        from . import day_use_payment_messages as links
        from . import day_use_stays as domain
    return links, domain, loyalty


@sensitive_variables()
def trusted_copy(function, stay):
    """Transform approved static markup before inserting any guest/property text."""
    property_copy = SimpleNamespace(
        **{f"name_{lang}": "{{listingname}}" for lang in ("ar", "en", "fr")}
    )

    class Copy:
        guest_name = "{{guestname}}"
        property = property_copy

        def __getattr__(self, key):
            return getattr(stay, key)

    text = function(Copy())
    text = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", text)
    values = {
        "guestname": stay.guest_name,
        "listingname": getattr(stay.property, "name_" + stay.guest_language, "")
        or stay.property.name_en,
    }
    return render.render_html(text, values, stay.guest_language)


def image(purpose, scenario, language):
    if purpose == "loyalty_invitation":
        from apps.loyalty.enrollment_invitation import invitation_image

        raw = invitation_image(language)
        ref = f"site01-loyalty-enrollment-{language}-v3"
    elif scenario == "07" and purpose == "payment_link":
        from .morning_payment_messages import notice_image

        raw = notice_image(language)
        ref = f"site01-morning-payment-{language}"
    else:
        return None
    return {
        "asset_ref": ref,
        "asset_sha256": hashlib.sha256(raw).hexdigest(),
        "asset_mime": "image/png",
    }


def payment_link(packet, stay, event, next_check=None):
    from uuid import uuid4

    from .telegram_stay_journey import accepted_core, plan, send_packet
    from .telegram_stay_source import snapshot

    scenario = packet["scenario"]
    if not accepted_core(stay, scenario):
        return {**packet, "decision": "wait", "reason": "core_acceptance_pending"}
    if stay.paid_at:
        pay_events = source.SOURCES[scenario][2]
        candidate = next(
            (
                e
                for e in pay_events.objects.filter(stay=stay).order_by("-created_at")[:4]
                if source.SOURCES[scenario][5].current(e)
            ),
            None,
        )
        if candidate:
            with transaction.atomic():
                paid = snapshot(
                    {
                        "contract": "tg39_stay_source_request_v1",
                        "scenario": scenario,
                        "operation": "next",
                        "request_id": str(uuid4()),
                        "event_id": str(candidate.pk),
                        "stay_key": packet["stay_key"],
                    }
                )
            return financial(paid, stay, candidate)
        return {**packet, "decision": "wait", "reason": "verified_payment_event_pending"}
    links, domain, _ = modules(scenario)
    try:
        links.require_unpaid(stay)
        domain.verify_window(
            stay.property,
            stay.starts_at,
            stay.ends_at,
            guests=stay.guests,
            allow_started=True,
            allow_elapsed=scenario == "07",
            linked_stay=stay,
        )
        if (
            domain.local_inflight_conflict(stay.property, stay.starts_at, stay.ends_at)
            or domain.active_stays(
                stay.property,
                stay.starts_at,
                stay.ends_at + timedelta(hours=3),
                exclude=stay.pk,
            ).exists()
        ):
            raise Denied
    except (MorningStayError, DayUseStayError, ValidationError):
        return {**packet, "decision": "hold", "reason": "payment_eligibility_unverified"}
    row = plan(packet, "payment_link", 5, event.created_at, stay.ends_at)
    if row.state == "accepted":
        return {
            **packet,
            "decision": "wait",
            "reason": "awaiting_payment",
            "next_check_at": (next_check or stay.ends_at).isoformat(),
        }
    return send_packet(row, packet, stay)


@sensitive_variables()
def content(row, stay):
    links, _, loyalty = modules(row.scenario)
    if row.purpose == "payment_link":
        fn = links.render_message
    elif row.purpose == "payment_success":
        fn = source.SOURCES[row.scenario][5].render_success
    else:
        fn = loyalty.caption
    value = render.message(
        stay.guest_language,
        trusted_copy(fn, stay),
        image(row.purpose, row.scenario, stay.guest_language),
    )
    return value, render.sha("approved-existing-financial-copy-v1:" + row.purpose)


def financial(packet, stay, event):
    from .telegram_stay_journey import accepted_core, plan, send_packet

    scenario = packet["scenario"]
    stay = source.SOURCES[scenario][0].objects.select_related("property").get(pk=stay.pk)
    event = source.SOURCES[scenario][2].objects.get(pk=event.pk, stay=stay)
    event.stay = stay
    if not source.SOURCES[scenario][5].current(event) or not accepted_core(stay, scenario):
        return {**packet, "decision": "wait", "reason": "current_paid_core_required"}
    row = plan(packet, "payment_success", 6, event.created_at, stay.ends_at)
    if row.state != "accepted":
        return send_packet(row, packet, stay)
    return loyalty_packet(packet, stay, event)


def loyalty_packet(packet, stay, event):
    from apps.loyalty import invitation_registry as registry
    from apps.loyalty.invitation_api import ready

    from .telegram_stay_journey import plan, send_packet

    if not ready():
        return {**packet, "decision": "wait", "reason": "programme_unavailable"}
    scenario = packet["scenario"]
    row = plan(packet, "loyalty_invitation", 7, event.created_at, stay.ends_at)
    if row.state == "accepted":
        return {**packet, "decision": "done", "reason": "paid_journey_accepted"}
    phone = registry.normalize_phone(stay.guest_phone)
    key = packet["stay_key"] if scenario == "07" else registry.day_use_booking_key(stay)
    with registry.invitation_lock():
        current = TelegramStayMessage.objects.select_for_update().get(pk=row.pk)
        invitation_id = current.booking_basis.get("loyalty_invitation_id")
        if invitation_id:
            from apps.loyalty.models import JoinInvitation

            invitation = JoinInvitation.objects.get(pk=invitation_id)
            if (
                invitation.channel != "telegram"
                or invitation.reservation_key != key
                or invitation.state not in {"unknown", "accepted"}
            ):
                raise Denied
        else:
            decision, invitation = registry.claim(
                key=key,
                phone=phone,
                email=stay.guest_email,
                customer_id=None,
                language=stay.guest_language,
                event_id=key + ":paid-invitation-v1",
                source="morning07" if scenario == "07" else "day_use08",
                origin="website",
                channel="telegram",
            )
            if decision != "eligible":
                return {**packet, "decision": "done", "reason": "loyalty_" + decision}
            decision = registry.consume(invitation, phone=phone, telegram_stay_message=current)
            if decision != "eligible":
                return {**packet, "decision": "done", "reason": "loyalty_" + decision}
            current.booking_basis = {
                **current.booking_basis,
                "loyalty_invitation_id": str(invitation.pk),
            }
            current.save(update_fields=["booking_basis", "updated_at"])
    return send_packet(row, packet, stay)


def payable_link(stay, scenario):
    """A source-prepared release permits checkout, independently of message status.

    Like the original queued/sending/uncertain delivery resource, preparation
    precedes the provider POST. A fast click must not race the receipt import.
    HyperPay and current booking/amount/availability checks remain with checkout.
    """
    from .telegram_stay_journey import accepted_core

    links, _, _ = modules(scenario)
    rows = list(
        TelegramStayMessage.objects.filter(
            scenario=scenario,
            stay_id=stay.pk,
            revision=stay.communication_revision,
            purpose="payment_link",
            state__in=["planned", "uncertain", "accepted"],
            expires_at__gt=timezone.now(),
            booking_basis__payment_fingerprint=links.fingerprint(stay),
        )
        .exclude(content_sha256="")
        .exclude(binding_fingerprint="")
        .order_by("created_at")[:2]
    )
    return bool(
        stay.ends_at > timezone.now()
        and len(rows) == 1
        and accepted_core(stay, scenario, fingerprint=rows[0].binding_fingerprint)
    )


def validate_loyalty_permit(invitation, message, phone):
    from apps.loyalty import invitation_registry as registry

    from .telegram_stay_journey import accepted_core

    scenario = message.scenario
    stay = (
        source.SOURCES[scenario][0]
        .objects.select_for_update()
        .select_related("property")
        .get(pk=message.stay_id)
    )
    event = source.SOURCES[scenario][2].objects.get(pk=message.event_id, stay=stay)
    event.stay = stay
    key = "morning07:" + str(stay.pk) if scenario == "07" else registry.day_use_booking_key(stay)
    if (
        invitation.channel != "telegram"
        or invitation.origin != "website"
        or invitation.source != ("morning07" if scenario == "07" else "day_use08")
        or invitation.reservation_key != key
        or invitation.phone != phone
        or phone != registry.normalize_phone(stay.guest_phone)
        or invitation.email.casefold() != stay.guest_email.casefold()
        or invitation.language != stay.guest_language
        or message.purpose != "loyalty_invitation"
        or stay.communication_revision != message.revision
        or stay.ends_at <= timezone.now()
        or not source.SOURCES[scenario][5].current(event)
        or not accepted_core(stay, scenario)
        or not TelegramStayMessage.objects.filter(
            scenario=scenario,
            stay_id=stay.pk,
            event_id=event.pk,
            revision=message.revision,
            purpose="payment_success",
            state="accepted",
        ).exists()
    ):
        raise ValidationError("telegram_stay_invitation_not_verified")
