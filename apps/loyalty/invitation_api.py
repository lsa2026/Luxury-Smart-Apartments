"""n8n coordinates provider delivery; these endpoints never send messages."""

import re
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.accounts.access import operations_access_required, require_operations_owner
from apps.reservations.sama_booking_api import payload, response

from . import invitation_registry as registry
from . import telegram_invitations
from .invitation_auth import authenticated
from .models import InvitationAPICredential, JoinInvitation

FIELDS = (
    "reservation_key",
    "phone",
    "email",
    "language",
    "guest_name",
    "source_platform",
    "event_id",
)


def ready():
    from .views import approved_terms

    return bool(
        settings.LOYALTY_ENABLED
        and settings.LOYALTY_UI_ENABLED
        and approved_terms()
        and not settings.LOYALTY_UI_PREVIEW
        and not settings.LOYALTY_PILOT_ONLY
    )


def snapshot(row, decision="duplicate", *, allow=False):
    result = {"decision": decision, "send_allowed": allow, "retry_provider_post": False}
    if row:
        result.update(
            invitation_id=str(row.pk),
            provider_reference=row.provider_reference,
            status=row.state,
            provider_id=row.provider_id,
            outcomes=row.outcomes,
            language=row.language,
        )
        if row.channel == "telegram":
            result.update(channel="telegram", provider_receipt_lookup=False)
        elif row.channel == "workgroup_manual":
            from apps.reservations.telegram_workgroup import WORKGROUP_ID

            result.update(
                channel="workgroup_manual",
                workgroup_id=WORKGROUP_ID,
                handoff_allowed=allow,
                send_allowed=False,
                guest_accepted=False,
                delivered=False,
                read=False,
                provider_receipt_lookup=False,
            )
    return result


def limited():
    key = "loyalty:n8n-invitation-api:v1"
    if cache.add(key, 1, timeout=60):
        return False
    try:
        return cache.incr(key) > 120
    except ValueError:
        return True


def checked(view):
    @authenticated("POST")
    def invoke(request):
        if limited():
            return response("rate_limited", 429)
        try:
            return view(request)
        except ValidationError as exc:
            return response("invalid_request", 409, reason=exc.messages[0])
        except (ValueError, TypeError, KeyError):
            return response("invalid_request", 400)

    return invoke


@authenticated("GET")
def health(request):
    # An existing table is required, not merely a new route returning HTTP 200.
    JoinInvitation.objects.exists()
    return response(
        "ready",
        version="loyalty-invitations-v1",
        programme_ready=ready(),
        capabilities=["claim", "preflight", "record", "status"],
        sends_messages=False,
    )


@operations_access_required
@require_GET
@never_cache
def readiness(request):
    require_operations_owner(request.user)
    JoinInvitation.objects.exists()
    return response(
        "ready",
        version="loyalty-invitations-v1",
        programme_ready=ready(),
        authentication_configured=InvitationAPICredential.objects.filter(
            revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).exists(),
        credential_setup_url="/admin/operations/loyalty-invitation-credential/",
        sends_messages=False,
        creates_memberships=False,
    )


@checked
def claim(request):
    if not ready():
        return response("programme_unavailable", 503)
    data = payload(request, FIELDS + ("channel",))
    if data is None:
        data = payload(request, FIELDS)
    channel = data.pop("channel", "whatsapp") if data else "whatsapp"
    if channel not in {"whatsapp", "telegram", "workgroup_manual"}:
        return response("invalid_channel", 400)
    from apps.reservations.telegram_workgroup import enabled as workgroup_enabled

    if channel == "workgroup_manual" and not workgroup_enabled():
        return response("workgroup_handoff_closed", 503)
    from apps.reservations.telegram_workgroup import manual_mode

    if channel != "workgroup_manual" and manual_mode():
        return response("direct_guest_closed", 409)
    if not data or any(not isinstance(data[k], str) for k in FIELDS):
        return response("invalid_request", 400)
    limits = dict(zip(FIELDS, (80, 80, 254, 2, 150, 80, 150), strict=True))
    if any(len(data[k]) > limits[k] for k in FIELDS) or data["language"] not in {"ar", "en", "fr"}:
        return response("invalid_request", 400)
    source, event_id = data["source_platform"].strip(), data["event_id"].strip()
    if not source or not event_id or source.casefold() in {"website", "direct_website", "site"}:
        return response("invalid_source_or_event", 400)
    email = data["email"].strip().casefold()
    if email:
        validate_email(email)
    phone = registry.normalize_phone(data["phone"])
    with registry.invitation_lock():
        key, booking = registry.canonical_booking(data["reservation_key"])
        chat = telegram_invitations.target(key, phone) if channel == "telegram" else None
        if booking and (
            booking.source_type == "direct_website"
            or booking.is_test
            or booking.normalized_status in booking.CLOSED_STATUSES
        ):
            return response("blocked", **snapshot(None, "blocked"))
        customer = (
            booking.booking_intent.customer_id if booking and booking.booking_intent_id else None
        )
        decision, row = registry.claim(
            key=key,
            phone=phone,
            email=email,
            customer_id=customer,
            language=data["language"],
            event_id=event_id,
            source=source,
            origin="n8n",
            channel=channel,
        )
        if decision == "eligible" and row.channel != channel:
            raise ValidationError("event_channel_conflict")
        if decision == "eligible" and channel == "telegram":
            target_digest = telegram_invitations.digest(chat)
            if row.telegram_target_digest and row.telegram_target_digest != target_digest:
                raise ValidationError("telegram_binding_changed")
            row.telegram_target_digest = target_digest
            row.save(update_fields=["telegram_target_digest", "updated_at"])
        result = snapshot(row, decision)
        if decision == "eligible":
            result.update(
                ticket=registry.ticket(row),
                expires_at=row.expires_at.isoformat(),
                recipient_phone=row.phone,
            )
            if channel == "telegram":
                result["recipient_chat_id"] = chat
        return response("ok", **result)


def row_from(data):
    return JoinInvitation.objects.select_for_update().filter(pk=UUID(data["invitation_id"])).first()


@checked
def preflight(request):
    if not ready():
        return response("programme_unavailable", 503)
    data = payload(request, ("invitation_id", "ticket", "phone"))
    if not data or any(not isinstance(v, str) or len(v) > 2000 for v in data.values()):
        return response("invalid_request", 400)
    with registry.invitation_lock():
        row = row_from(data)
        if not row or row.origin != "n8n":
            return response("not_found", 404)
        from apps.reservations.telegram_workgroup import enabled as workgroup_enabled
        from apps.reservations.telegram_workgroup import manual_mode

        if manual_mode() and row.channel != "workgroup_manual":
            return response("direct_guest_closed", 409)
        if row.channel == "workgroup_manual":
            if not workgroup_enabled():
                return response("workgroup_handoff_closed", 503)
        chat = telegram_invitations.verify(row) if row.channel == "telegram" else None
        decision = registry.consume(
            row, phone=registry.normalize_phone(data["phone"]), proof=data["ticket"]
        )
        result = snapshot(row, decision, allow=decision == "eligible")
        if decision == "eligible":
            result.update(
                recipient_phone=row.phone,
                send_within_seconds=60,
                send_expires_at=(timezone.now() + timedelta(seconds=60)).isoformat(),
            )
            if chat:
                result["recipient_chat_id"] = chat
            if row.channel == "workgroup_manual":
                result.pop("send_within_seconds", None)
                result.pop("send_expires_at", None)
                result.update(
                    handoff_within_seconds=60,
                    handoff_expires_at=(timezone.now() + timedelta(seconds=60)).isoformat(),
                )
        return response("ok", **result)


@checked
def record(request):
    data = payload(request, ("invitation_id", "provider_reference", "status", "provider_id"))
    if not data or any(not isinstance(v, str) or len(v) > 150 for v in data.values()):
        return response("invalid_request", 400)
    if data["status"] not in {"accepted", "unknown", "delivered", "read", "failed", "handed_off"}:
        return response("invalid_status", 400)
    if data["provider_id"] and not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", data["provider_id"]):
        return response("invalid_provider_id", 400)
    with registry.invitation_lock():
        row = row_from(data)
        if not row or row.origin != "n8n":
            return response("not_found", 404)
        if row.channel == "workgroup_manual" and (
            data["status"] not in {"handed_off", "unknown"}
            or data["status"] == "handed_off"
            and not re.fullmatch(r"[1-9][0-9]{0,18}", data["provider_id"])
        ):
            return response("invalid_workgroup_outcome", 400)
        if row.channel != "workgroup_manual" and data["status"] == "handed_off":
            return response("invalid_channel_outcome", 400)
        if row.channel == "telegram" and (
            data["status"] not in {"accepted", "unknown"}
            or data["provider_id"]
            and not re.fullmatch(r"[1-9][0-9]{0,18}", data["provider_id"])
        ):
            return response("invalid_telegram_outcome", 400)
        if data["provider_reference"] != row.provider_reference:
            return response("reference_conflict", 409)
        registry.record(row, data["status"], data["provider_id"])
        return response("ok", **snapshot(row))


@checked
def status(request):
    data = payload(request, ("invitation_id",))
    key_data = payload(request, ("reservation_key",)) if data is None else None
    with registry.invitation_lock():
        if data:
            row = row_from(data)
        elif key_data:
            key, _ = registry.canonical_booking(key_data["reservation_key"])
            row = JoinInvitation.objects.filter(reservation_key=key).first()
        else:
            return response("invalid_request", 400)
        if not row:
            return response("not_found", 404, send_allowed=False)
        return response("ok", **snapshot(row))
