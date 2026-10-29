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
    data = payload(request, FIELDS)
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
            channel="whatsapp",
        )
        result = snapshot(row, decision)
        if decision == "eligible":
            result.update(
                ticket=registry.ticket(row),
                expires_at=row.expires_at.isoformat(),
                recipient_phone=row.phone,
            )
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
        return response("ok", **result)


@checked
def record(request):
    data = payload(request, ("invitation_id", "provider_reference", "status", "provider_id"))
    if not data or any(not isinstance(v, str) or len(v) > 150 for v in data.values()):
        return response("invalid_request", 400)
    if data["status"] not in {"accepted", "unknown", "delivered", "read", "failed"}:
        return response("invalid_status", 400)
    if data["provider_id"] and not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", data["provider_id"]):
        return response("invalid_provider_id", 400)
    with registry.invitation_lock():
        row = row_from(data)
        if not row or row.origin != "n8n":
            return response("not_found", 404)
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
