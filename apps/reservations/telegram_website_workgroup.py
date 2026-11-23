"""Fixed workgroup copies for website payment and administrator loyalty flows."""

import base64
import hashlib
import html
import json
import logging
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from . import telegram_workgroup as group
from .telegram_authority import Denied
from .telegram_backend import Backend, Unavailable
from .telegram_stay_models import TelegramWebsiteHandoff
from .telegram_stay_render import sha

logger = logging.getLogger(__name__)


def digest(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def as_html(body, guest_name=""):
    # Interpret only approved static WhatsApp bold after escaping all HTML.
    marker = "LSA_LITERAL_GUEST_NAME_6F39"
    if guest_name:
        body = body.replace(guest_name, marker)
    text = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", html.escape(body, quote=False))
    return text.replace(marker, html.escape(guest_name, quote=False))


def admitted(created):
    try:
        at = parse_datetime(getattr(settings, "TELEGRAM_WORKGROUP_CUTOVER_AT", ""))
        return bool(at and timezone.is_aware(at) and at <= created <= timezone.now())
    except (TypeError, ValueError):
        return False


@sensitive_variables()
def source_copy(kind, source_id):
    if kind == "payment_request":
        from apps.accounts.phone_messages import phone_logo
        from apps.payments.hyperpay.exceptions import HyperPayError
        from apps.payments.models import PaymentRequestDelivery
        from apps.payments.payment_link_tasks import payment_message
        from apps.payments.payment_links import validate_open

        delivery = PaymentRequestDelivery.objects.select_related(
            "request__reservation__booking_intent"
        ).get(pk=source_id)
        request = delivery.request
        if (
            request.environment != "production"
            or request.environment != settings.HYPERPAY_ENVIRONMENT
        ):
            raise Denied
        if delivery.kind == "link":
            if not settings.RESERVATION_PAYMENT_LINKS_ENABLED:
                raise Denied
            try:
                validate_open(request)
            except HyperPayError as exc:
                raise Denied from exc
        elif delivery.kind != "receipt" or request.status != "paid" or not request.paid_at:
            raise Denied
        if delivery.status not in {"queued", "handoff_hold", "handoff_wait"}:
            raise Denied
        intent = request.reservation.booking_intent
        body, image, language = (
            payment_message(request, delivery.kind),
            phone_logo(),
            request.language,
        )
        phone, name = (
            intent.guest_phone,
            f"{intent.guest_first_name} {intent.guest_last_name}".strip(),
        )
        basis = {
            "request": str(request.pk),
            "fingerprint": request.fingerprint,
            "amount": str(request.amount_sar),
            "currency": request.source_currency,
            "status": request.status,
            "kind": delivery.kind,
            "sequence": delivery.sequence,
            "modification": str(request.modification_request_id or ""),
        }
        purpose = "payment_link" if delivery.kind == "link" else "payment_success"
        created = delivery.created_at
    elif kind == "loyalty_invitation":
        from django.contrib.auth import get_user_model
        from django.core.exceptions import ValidationError

        from apps.accounts.access import is_operations_owner
        from apps.loyalty import booking_invitations as invitations
        from apps.loyalty import invitation_registry as registry
        from apps.loyalty.past_guest_offers import KEY

        from .models import Reservation

        booking_id, request_id = source_id.split(":", 1)
        booking = Reservation.objects.select_related("booking_intent__customer", "property").get(
            pk=uuid.UUID(booking_id)
        )
        row = invitations._request_row(booking, "whatsapp", request_id or None)
        operator = get_user_model().objects.filter(pk=row.get("operator")).first()
        if (
            row.get("status") not in {"queued", "handoff_wait", "handoff_hold"}
            or row.get("past_guest_offer") != KEY
            or not is_operations_owner(operator)
        ):
            raise Denied
        target = invitations._target(booking, "whatsapp")
        expected = (
            invitations._target_digest(target, "whatsapp")
            if request_id
            else invitations.digest(target)
        )
        if expected != row["target_digest"] or registry.member_exists(
            target["phone"], target["email"], target["customer"]
        ):
            raise Denied
        body = invitations.invitation_caption(
            target["language"], target["url"], target["guest_name"], target["reward"]
        )
        image, language, phone, name = (
            invitations.invitation_image(target["language"]),
            target["language"],
            target["phone"],
            target["guest_name"],
        )
        basis = {
            "target_digest": row["target_digest"],
            "shared": row["shared_invitation_id"],
            "provider_reference": row["provider_reference"],
            "requested_at": row["requested_at"],
        }
        created = parse_datetime(row["requested_at"])
        if not created:
            raise ValidationError("invalid_invitation_time")
        purpose = "loyalty_invitation"
    elif kind == "booking_event":
        from .telegram_booking_events import source

        body, image, language, phone, name, basis, created, purpose = source(source_id)
    else:
        raise Denied
    if (
        not admitted(created)
        or not image.startswith(b"\x89PNG\r\n\x1a\n")
        or len(image) > 300_000
        or language not in {"ar", "en", "fr"}
    ):
        raise Denied
    phone = group.canonical_phone(phone)
    caption = as_html(body, name)
    asset_sha = hashlib.sha256(image).hexdigest()
    content_sha = sha(
        json.dumps(
            [language, "HTML", "photo", caption, asset_sha],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return {
        "source_kind": kind,
        "source_id": source_id,
        "source_revision": digest(basis),
        "phone_hash": sha(phone.lstrip("+")),
        "guest_phone_e164": phone or None,
        "guest_name": name,
        "language": language,
        "purpose": purpose,
        "content_sha256": content_sha,
        "method": "photo",
        "caption_html": caption,
        "parse_mode": "HTML",
        "asset_sha256": asset_sha,
        "asset_mime": "image/png",
        "asset_base64": base64.b64encode(image).decode("ascii"),
        "not_before": created.isoformat(),
        "expires_at": (created + timedelta(minutes=15)).isoformat(),
    }


def metadata(row):
    return {
        "job_id": str(row.pk),
        "source_kind": row.source_kind,
        "source_id": row.source_id,
        "source_revision": row.source_revision,
        "job_key": row.job_key,
        "reference_id": row.reference_id,
        "phone_hash": row.phone_hash,
        "content_sha256": row.content_sha256,
    }


@sensitive_variables()
def authorize(scope, action, key, result):
    if not group.manual_mode() or action not in {"website_handoff", "website_handoff_status"}:
        raise Denied
    if action == "website_handoff" and not group.enabled():
        raise Denied
    row = TelegramWebsiteHandoff.objects.get(pk=uuid.UUID(scope["job"]))
    if key != str(row.pk):
        raise Denied
    packet = {"contract": "tg39_website_workgroup_packet_v1", **metadata(row)}
    if action == "website_handoff":
        if row.state != "dispatching" or not row.not_before <= timezone.now() < row.expires_at:
            logger.warning("Website handoff held job=%s stage=state_or_window", row.pk)
            raise Denied
        try:
            content = source_copy(row.source_kind, row.source_id)
        except (Denied, ValueError, ValidationError, ObjectDoesNotExist, KeyError, TypeError) as exc:
            logger.warning("Website handoff held job=%s stage=current_source", row.pk)
            raise Denied from exc
        changed = [
            k
            for k in ("source_revision", "phone_hash", "content_sha256", "asset_sha256")
            if content[k] != getattr(row, k)
        ]
        if changed:
            logger.warning(
                "Website handoff held job=%s stage=immutable_fields fields=%s", row.pk, changed
            )
            raise Denied
        packet.update(content)
    result.update(
        source_scope="website_handoff",
        dispatch_mode="workgroup_manual",
        workgroup_id=group.WORKGROUP_ID,
        packet=packet,
    )
    return result


def import_proof(row, proof):
    if not isinstance(proof, dict) or proof.get("available") is not True:
        return False
    if (
        proof.get("contract") != "tg39_workgroup_handoff_v1"
        or proof.get("channel") != "workgroup_manual"
        or proof.get("workgroup_id") != group.WORKGROUP_ID
        or proof.get("workgroup_binding_fingerprint") != group.fingerprint()
        or any(proof.get(k) is not False for k in ("guest_accepted", "delivered", "read"))
        or any(type(proof.get(k)) is not type(v) or proof[k] != v for k, v in metadata(row).items())
    ):
        raise Denied
    attempt = str(uuid.UUID(proof["attempt_id"]))
    if attempt != proof["attempt_id"] or row.attempt_id and str(row.attempt_id) != attempt:
        raise Denied
    if proof.get("status") in {"posting", "unknown"} and proof.get("api_accepted") is False:
        return False
    at = parse_datetime(proof.get("accepted_at", ""))
    ids = proof.get("provider_message_ids")
    if (
        proof.get("status") != "handed_off"
        or proof.get("api_accepted") is not True
        or not isinstance(ids, list)
        or not 1 <= len(ids) <= 64
        or any(not isinstance(x, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", x) for x in ids)
        or proof.get("provider_message_id") != ids[-1]
        or not at
        or not timezone.is_aware(at)
        or not row.created_at <= at <= timezone.now() + timedelta(seconds=5)
    ):
        raise Denied
    with transaction.atomic():
        current = TelegramWebsiteHandoff.objects.select_for_update().get(pk=row.pk)
        if current.state == "handed_off" and (
            str(current.attempt_id) != attempt
            or current.provider_message_id != ids[-1]
            or current.provider_message_ids != ids
        ):
            raise Denied
        current.state, current.attempt_id, current.provider_message_id, current.accepted_at = (
            "handed_off",
            attempt,
            ids[-1],
            at,
        )
        current.provider_message_ids = ids
        current.save(
            update_fields=[
                "state",
                "attempt_id",
                "provider_message_id",
                "provider_message_ids",
                "accepted_at",
                "updated_at",
            ]
        )
        finish(current)
    return True


def finish(row):
    if row.source_kind == "booking_event":
        from .telegram_refund_copy_requests import completion

        completion(row)
    if row.source_kind == "payment_request":
        from apps.payments.models import PaymentRequestDelivery

        PaymentRequestDelivery.objects.filter(
            pk=row.source_id, status__in=["queued", "handoff_wait", "handoff_hold"]
        ).update(status="handed_off", error_code="workgroup_manual_copy")
    elif row.source_kind == "loyalty_invitation":
        from apps.loyalty.booking_invitations import _finish

        booking, request_id = row.source_id.split(":", 1)
        _finish(
            booking,
            "handed_off",
            expected="handoff_wait",
            request_id=request_id or None,
            provider_id=row.provider_message_id,
        )


@sensitive_variables()
def deliver(kind, source_id):
    if not group.enabled():
        raise Denied
    content = source_copy(kind, str(source_id))
    key = "site01wg:" + kind + ":" + sha(str(source_id))
    with transaction.atomic():
        row, created = TelegramWebsiteHandoff.objects.get_or_create(
            job_key=key,
            defaults={
                "source_kind": kind,
                "source_id": str(source_id),
                "source_revision": content["source_revision"],
                "reference_id": "site01wg-" + sha(key),
                "phone_hash": content["phone_hash"],
                "content_sha256": content["content_sha256"],
                "asset_sha256": content["asset_sha256"],
                "not_before": parse_datetime(content["not_before"]),
                "expires_at": parse_datetime(content["expires_at"]),
            },
        )
        if not created:
            return row.state  # Reconciliation is status-only, never another provider POST.
        if not row.not_before <= timezone.now() < row.expires_at:
            row.state = "held"
            row.save(update_fields=["state", "updated_at"])
            return "held"
        row.state = "dispatching"
        row.save(update_fields=["state", "updated_at"])
    call(row, "website_handoff")
    row.refresh_from_db()
    return row.state


def call(row, action):
    client = None
    try:
        client = Backend()
        client.authority_context = {"kind": "website_handoff", "job": str(row.pk)}
        proof = client.call(action, str(row.pk))
        if import_proof(row, proof):
            return
    except (
        Denied,
        Unavailable,
        ValueError,
        KeyError,
        TypeError,
        ValidationError,
        ObjectDoesNotExist,
    ):
        pass
    finally:
        if client:
            client.close()
    TelegramWebsiteHandoff.objects.filter(pk=row.pk, updated_at=row.updated_at).exclude(
        state="handed_off"
    ).update(state="uncertain")


def recover():
    if not group.manual_mode():
        return
    if group.enabled():
        from .telegram_booking_events import dispatch

        for row_id in (
            TelegramWebsiteHandoff.objects.filter(source_kind="booking_event", state="planned")
            .order_by("created_at")
            .values_list("pk", flat=True)[:2]
        ):
            dispatch(row_id)
    for row in TelegramWebsiteHandoff.objects.filter(
        state__in=["dispatching", "uncertain"], probes__lt=10, next_probe_at__lte=timezone.now()
    ).order_by("created_at")[:2]:
        # A status read cannot create/replay any provider operation.
        row.probes += 1
        row.next_probe_at = timezone.now() + timedelta(seconds=min(600, 30 * row.probes))
        row.save(update_fields=["probes", "next_probe_at", "updated_at"])
        call(row, "website_handoff_status")
        row.refresh_from_db()
        if row.probes >= 10 and row.state != "handed_off":
            alert(row)


def alert(row):
    from apps.notifications.services.email import queue_email

    queue_email(
        message_type="integration_attention",
        recipient=settings.OPERATIONS_EMAIL,
        recipient_source="operations",
        recipient_reference="workgroup-copy:" + str(row.pk),
        language="ar",
        idempotency_key="workgroup-copy-held:" + str(row.pk),
    )


def deliver_payment(delivery_id):
    from apps.payments.models import PaymentRequestDelivery

    if not group.enabled():
        return {"status": "handoff_paused", "guest_accepted": False}

    with transaction.atomic():
        row = PaymentRequestDelivery.objects.select_for_update().get(pk=delivery_id)
        if row.status != "queued":
            return {"status": row.status}
        row.status = "handoff_wait"
        row.save(update_fields=["status"])
    try:
        status = deliver("payment_request", str(delivery_id))
    except (Denied, ValueError, ValidationError, ObjectDoesNotExist, KeyError, TypeError):
        status = "held"
    if status != "handed_off":
        PaymentRequestDelivery.objects.filter(pk=delivery_id, status="handoff_wait").update(
            status="handoff_hold", error_code="workgroup_copy_unconfirmed"
        )
    return {"status": status, "dispatch_mode": "workgroup_manual", "guest_accepted": False}


def deliver_loyalty(booking_id, request_id):
    try:
        status = deliver("loyalty_invitation", str(booking_id) + ":" + (request_id or ""))
    except (Denied, ValueError, ValidationError, ObjectDoesNotExist, KeyError, TypeError):
        status = "held"
    if status != "handed_off":
        # Keep handoff_wait for status-only recovery; never translate uncertainty
        # into a guest provider attempt or a repeat invitation.
        return {"status": "handoff_hold", "guest_accepted": False}
    return {"status": "handed_off", "guest_accepted": False}
