"""Shared invitation claims. No provider writes, membership changes or points."""

import re
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.accounts.models import VerifiedPhone
from apps.core.phone_numbers import normalize_phone_number
from apps.notifications.services.email import recipient_hmac
from apps.reservations.models import Reservation

from .models import JoinInvitation, Member
from .phone_gifts import phone_hash

SALT = "loyalty.n8n.invitation.claim.v1"
TTL = 300
IN_FLIGHT = {"queued", "sending", "accepted", "uncertain", "unknown", "delivered", "read"}


@contextmanager
def invitation_lock():
    # Same lock as enrollment: checks and claims cannot race an enrollment commit.
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(190009, 8)")
        yield


def keyed(value):
    return salted_hmac("loyalty.invitation-registry.v1", value, algorithm="sha256").hexdigest()


def canonical_booking(value):
    value = str(value).strip()
    if value.startswith("hostaway:"):
        value = value[9:]
    if re.fullmatch(r"[1-9][0-9]{0,17}", value):
        booking = (
            Reservation.objects.select_related("booking_intent")
            .filter(hostaway_reservation_id=int(value))
            .first()
        )
        return "hostaway:" + str(int(value)), booking
    if value.startswith("site:"):
        value = value[5:]
    try:
        identifier = UUID(value)
    except (ValueError, TypeError) as exc:
        raise ValidationError("invalid_reservation_key") from exc
    booking = Reservation.objects.select_related("booking_intent").filter(pk=identifier).first()
    if not booking:
        raise ValidationError("reservation_not_found")
    return booking_key(booking), booking


def booking_key(booking):
    return (
        "hostaway:" + str(booking.hostaway_reservation_id)
        if booking.hostaway_reservation_id
        else "site:" + str(booking.pk)
    )


def member_exists(phone, email="", customer_id=None):
    query = Q()
    if phone:
        query |= Q(customer_id__in=VerifiedPhone.objects.filter(phone=phone).values("user_id"))
    if email:
        # Email is suppression evidence only, never identity proof or account access.
        query |= Q(customer__email__iexact=email)
    if customer_id:
        query |= Q(customer_id=customer_id)
    return bool(query and Member.objects.filter(query).exists())


def legacy_exists(key, phone, email, *, exclude=None):
    """Retain old manual receipts as evidence; do not backfill or rewrite history."""
    keys = (
        "owner_join_invitation",
        "owner_join_invitation_email",
        "owner_join_invitation_requests",
        "owner_join_invitation_email_requests",
    )
    query = Q()
    for field in keys:
        query |= Q(**{"loyalty_state__has_key": field})
    for booking in (
        Reservation.objects.filter(query)
        .only("id", "hostaway_reservation_id", "loyalty_state")
        .iterator()
    ):
        same_booking = booking_key(booking) == key
        for channel, state_key, requests_key in (
            ("whatsapp", keys[0], keys[2]),
            ("email", keys[1], keys[3]),
        ):
            rows = [("", booking.loyalty_state.get(state_key, {}))]
            rows += list(booking.loyalty_state.get(requests_key, {}).items())
            for request_id, row in rows:
                if exclude == (str(booking.pk), channel, request_id):
                    continue
                if row.get("status") not in IN_FLIGHT:
                    continue
                if (
                    same_booking
                    or (phone and row.get("phone_digest") == phone_hash(phone))
                    or (email and row.get("recipient_digest") == recipient_hmac(email))
                ):
                    return True
    return False


def competing(key, phone, email, customer_id=None, *, exclude_id=None):
    query = Q(reservation_key=key)
    if phone:
        query |= Q(phone_digest=phone_hash(phone))
    if email:
        query |= Q(email_digest=recipient_hmac(email))
    if customer_id:
        query |= Q(customer_id=customer_id)
    rows = JoinInvitation.objects.filter(query).exclude(pk=exclude_id)
    for row in rows:
        if row.state not in {"blocked", "queue_failed"}:
            # Unused claims expire; anything that could have reached a provider never does.
            if row.state == "claimed" and row.expires_at <= timezone.now():
                continue
            return row
    return None


def claim(
    *,
    key,
    phone,
    email,
    customer_id,
    language,
    event_id,
    source,
    origin,
    channel,
    exclude_legacy=None,
):
    """Caller holds invitation_lock. Same booking always retains its invitation UUID."""
    event_key = keyed(origin + ":" + source + ":" + event_id)
    old_event = JoinInvitation.objects.filter(event_key=event_key).first()
    if old_event and old_event.reservation_key != key:
        raise ValidationError("event_conflict")
    row = JoinInvitation.objects.filter(reservation_key=key).first()
    if member_exists(phone, email, customer_id):
        return "member", row
    if not phone and channel == "whatsapp":
        return "phone_missing", row
    if row and not (
        row.state in {"blocked", "queue_failed"}
        or row.state == "claimed"
        and row.expires_at <= timezone.now()
        or row.state == "claimed"
        and row.event_key == event_key
    ):
        return "duplicate", row
    if competing(key, phone, email, customer_id, exclude_id=row.pk if row else None):
        return "duplicate", None
    if legacy_exists(key, phone, email, exclude=exclude_legacy):
        return "duplicate", row
    if (
        row
        and row.state == "claimed"
        and row.event_key == event_key
        and row.expires_at > timezone.now()
    ):
        if row.phone != phone or row.email != email or row.language != language:
            raise ValidationError("event_payload_conflict")
        return "eligible", row
    values = dict(
        phone=phone,
        email=email,
        phone_digest=phone_hash(phone) if phone else "",
        email_digest=recipient_hmac(email) if email else "",
        customer_id=customer_id,
        language=language,
        event_key=event_key,
        source=source,
        origin=origin,
        channel=channel,
        state="claimed",
        expires_at=timezone.now() + timedelta(seconds=TTL),
    )
    if row:
        for name, value in values.items():
            setattr(row, name, value)
        row.generation += 1
        row.save()
    else:
        row = JoinInvitation.objects.create(reservation_key=key, **values)
    return "eligible", row


def ticket(row):
    return signing.dumps(
        {"id": str(row.pk), "phone": row.phone_digest, "generation": row.generation}, salt=SALT
    )


def consume(row, *, phone, proof=None, exclude_legacy=None):
    """One send permit only. Lost preflight responses MUST NOT trigger a provider POST."""
    if proof is not None:
        try:
            data = signing.loads(proof, salt=SALT, max_age=TTL)
        except signing.BadSignature as exc:
            raise ValidationError("ticket_invalid_or_expired") from exc
        if data != {"id": str(row.pk), "phone": row.phone_digest, "generation": row.generation}:
            raise ValidationError("ticket_mismatch")
    if row.phone != phone:
        raise ValidationError("phone_changed")
    _, booking = canonical_booking(row.reservation_key)
    current_customer = (
        booking.booking_intent.customer_id if booking and booking.booking_intent_id else None
    )
    if member_exists(row.phone, row.email, row.customer_id) or member_exists(
        row.phone, row.email, current_customer
    ):
        if row.state == "claimed":
            row.state = "blocked"
            row.save(update_fields=["state", "updated_at"])
        return "member"
    if row.state != "claimed":
        return "duplicate"
    if (
        row.origin == "n8n"
        and booking
        and (
            booking.source_type == "direct_website"
            or booking.is_test
            or booking.normalized_status in booking.CLOSED_STATUSES
        )
    ):
        row.state = "blocked"
        row.save(update_fields=["state", "updated_at"])
        return "blocked"
    if row.expires_at <= timezone.now():
        raise ValidationError("claim_expired")
    if competing(
        row.reservation_key, row.phone, row.email, row.customer_id, exclude_id=row.pk
    ) or legacy_exists(row.reservation_key, row.phone, row.email, exclude=exclude_legacy):
        row.state = "blocked"
        row.save(update_fields=["state", "updated_at"])
        return "duplicate"
    row.state = "unknown"
    row.dispatched_at = timezone.now()
    row.save(update_fields=["state", "dispatched_at", "updated_at"])
    return "eligible"


def record(row, state, provider_id=""):
    if state not in {
        "accepted",
        "unknown",
        "delivered",
        "read",
        "failed",
        "queue_failed",
        "blocked",
    }:
        raise ValidationError("invalid_status")
    if state in {"accepted", "delivered", "read"} and row.origin == "n8n" and not provider_id:
        raise ValidationError("provider_id_required")
    if row.provider_id and provider_id and row.provider_id != provider_id:
        raise ValidationError("provider_id_conflict")
    if row.state == "claimed" and state not in {"queue_failed", "blocked"}:
        raise ValidationError("preflight_required")
    ranks = {"unknown": 0, "failed": 0, "accepted": 1, "delivered": 2, "read": 3}
    history = {"status": state, "at": timezone.now().isoformat()}
    if history["status"] not in {h["status"] for h in row.outcomes}:
        row.outcomes = row.outcomes + [history]
    if ranks.get(state, 0) >= ranks.get(row.state, 0):
        row.state = state
    row.provider_id = row.provider_id or provider_id
    row.save(update_fields=["state", "outcomes", "provider_id", "updated_at"])


def normalize_phone(value):
    if not value:
        return ""
    try:
        return normalize_phone_number(value, default_region="SA")
    except (ValueError, TypeError):
        return ""
