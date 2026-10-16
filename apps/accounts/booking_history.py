"""Explicit AND-match history recovery. No provider writes or reward accrual."""

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.html import strip_tags

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayResponseError
from apps.integrations.hostaway.reservation_validators import (
    normalize_hostaway_reservation_status,
    validate_reservation_response,
)
from apps.properties.models import Property
from apps.reservations.models import BookingIntent, Reservation

from .models import BookingHistoryImport, CustomerProfile, GuestBookingHistory, VerifiedPhone

ACTIVE = {"queued", "running"}
MAX_PAGES = 1000


@dataclass(frozen=True)
class Identity:
    email: str
    phone: str
    digest: str


def canonical_phone(value):
    # No suffix matching and no guessed national country. An old ambiguous
    # national number needs an administrator to correct its international form.
    if not isinstance(value, str) or len(value) > 80:
        return None
    value = unicodedata.normalize("NFKC", value)
    value = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value)
    if not re.fullmatch(r"[+0-9\s().-]+", value):
        return None
    value = re.sub(r"[\s().-]", "", value)
    value = "+" + value[2:] if value.startswith("00") else value
    if not value.startswith("+"):
        value = "+" + value
    try:
        return normalize_phone_number(value)
    except InvalidPhoneNumber:
        return None


def identity_for(user):
    if not user.is_active:
        return None
    email = (user.email or "").strip().casefold()
    try:
        validate_email(email)
    except ValidationError:
        return None
    if not CustomerProfile.objects.filter(user=user, email_verified_at__isnull=False).exists():
        return None
    binding = VerifiedPhone.objects.filter(user=user).first()
    phone = canonical_phone(binding.phone) if binding else None
    if not phone:
        return None
    digest = salted_hmac(
        "guest-history-identity.v1", f"{user.pk}:{email}:{phone}:{binding.version}"
    )
    return Identity(email, phone, digest.hexdigest())


def matches(email, phone, identity):
    return bool(
        isinstance(email, str)
        and email.strip().casefold() == identity.email
        and canonical_phone(phone) == identity.phone
    )


def claim_local(user, identity):
    """Caller holds the user lock. Assign only unowned exact-pair site records.

    Do not call claim_reservation or reward reconciliation: displaying history
    is not a new stay/payment/arrival event.
    """
    count = 0
    intents = (
        BookingIntent.objects.select_for_update(of=("self",))
        .filter(
            customer__isnull=True, guest_email__iexact=identity.email, reservation__isnull=False
        )
        .only("pk", "guest_email", "guest_phone")
    )
    for intent in intents:
        if matches(intent.guest_email, intent.guest_phone, identity):
            changed = BookingIntent.objects.filter(pk=intent.pk, customer__isnull=True).update(
                customer=user
            )
            if changed:
                _audit_link(user, "BookingIntent", intent.pk)
            count += changed
    return count


def _audit_link(user, kind, reference):
    from apps.notifications.models import AuditLog

    AuditLog.objects.create(
        actor_user=user,
        action="account.booking_history_linked",
        object_type=kind,
        object_reference=str(reference),
        summary="Booking history linked by verified email and phone.",
        metadata={"source": "verified_identity_pair"},
    )


def start_import(user):
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=user.pk)
        identity = identity_for(user)
        if not identity:
            return None, "verification_required"
        job = BookingHistoryImport.objects.select_for_update().filter(user=user).first()
        now = timezone.now()
        if job and job.status in ACTIVE and job.updated_at > now - timedelta(minutes=5):
            return job, "running"
        if job and job.requested_at > now - timedelta(seconds=60):
            return job, "cooldown"
        if not job:
            job = BookingHistoryImport(user=user)
        job.generation = uuid.uuid4()
        job.identity_digest = identity.digest
        job.status, job.cursor, job.seen_cursors = "queued", None, []
        job.pages = job.conflicts = job.skipped = 0
        job.added = claim_local(user, identity)
        job.lease = job.locked_until = None
        job.error_code = ""
        job.requested_at = now
        job.save()
        transaction.on_commit(lambda: dispatch(job.pk, job.generation))
        return job, "started"


def dispatch(job_id, generation):
    from .tasks import import_booking_history_page

    try:
        import_booking_history_page.apply_async(args=[str(job_id), str(generation)], countdown=1)
    except Exception:
        # Never print broker URLs, contact data, raw exceptions or provider rows.
        BookingHistoryImport.objects.filter(
            pk=job_id, generation=generation, status__in=ACTIVE
        ).update(status="partial", error_code="queue_unavailable", updated_at=timezone.now())


def _safe_text(value, limit):
    return strip_tags(value).strip()[:limit] if isinstance(value, str) else ""


def _project(row):
    snapshot = validate_reservation_response({"status": "success", "result": row})
    if snapshot.reservation_id != row["id"] or snapshot.total_price < 0:
        raise HostawayResponseError("Invalid matched booking identity or total.")
    if snapshot.total_price >= 10**10 or snapshot.guests > 32767:
        raise HostawayResponseError("Matched booking values out of bounds.")
    # Include hidden/maintenance units in old history, without publishing them.
    properties = list(Property.objects.filter(hostaway_listing_map_id=snapshot.listing_map_id)[:2])
    return {
        "property": properties[0] if len(properties) == 1 else None,
        "property_name": _safe_text(row.get("listingName") or row.get("listingMapName"), 200),
        "channel_id": snapshot.channel_id,
        "channel_name": _safe_text(row.get("channelName") or row.get("source"), 100),
        "normalized_status": normalize_hostaway_reservation_status(snapshot.status),
        "check_in": snapshot.check_in,
        "check_out": snapshot.check_out,
        "guests": snapshot.guests,
        "currency": snapshot.currency,
        "total_price": snapshot.total_price,
    }


def _store_match(user, identity, job, row):
    values = _project(row)
    existing = (
        Reservation.objects.select_related("booking_intent")
        .filter(hostaway_reservation_id=row["id"])
        .first()
    )
    intent = existing.booking_intent if existing and existing.booking_intent_id else None
    if intent and intent.customer_id and intent.customer_id != user.pk:
        return 0, 1
    if intent and intent.customer_id == user.pk:
        return 0, 0  # Original site card keeps its payment/management workflow.
    if intent and matches(intent.guest_email, intent.guest_phone, identity):
        changed = BookingIntent.objects.filter(pk=intent.pk, customer__isnull=True).update(
            customer=user
        )
        if changed:
            _audit_link(user, "BookingIntent", intent.pk)
        return changed, int(not changed)
    record, created = GuestBookingHistory.objects.get_or_create(
        hostaway_reservation_id=row["id"],
        defaults={
            **values,
            "user": user,
            "identity_digest": identity.digest,
            "last_seen_generation": job.generation,
        },
    )
    if record.user_id != user.pk:
        return 0, 1
    if created:
        _audit_link(user, "GuestBookingHistory", record.pk)
    GuestBookingHistory.objects.filter(pk=record.pk, user=user).update(
        **values,
        identity_digest=identity.digest,
        last_seen_generation=job.generation,
        checked_at=timezone.now(),
        is_visible=True,
    )
    return int(created), 0


def run_page(job_id, generation, *, client_factory=HostawayClient):
    """One bounded page per task, cursor and result committed together."""
    with transaction.atomic():
        # Use the same user -> job lock order as start_import.
        user_id = (
            BookingHistoryImport.objects.filter(pk=job_id).values_list("user_id", flat=True).first()
        )
        if not user_id:
            return "obsolete"
        user = get_user_model().objects.select_for_update().get(pk=user_id)
        job = BookingHistoryImport.objects.select_for_update().get(pk=job_id)
        now = timezone.now()
        if str(job.generation) != str(generation) or job.status not in ACTIVE:
            return "obsolete"
        if job.locked_until and job.locked_until > now:
            return "busy"
        identity = identity_for(user)
        if not identity or not constant_time_compare(identity.digest, job.identity_digest):
            job.status, job.error_code = "partial", "identity_changed"
            job.save(update_fields=["status", "error_code", "updated_at"])
            return "partial"
        lease = uuid.uuid4()
        job.lease, job.locked_until = lease, now + timedelta(seconds=45)
        job.status = "running"
        job.save(update_fields=["lease", "locked_until", "status", "updated_at"])
        cursor = job.cursor
    # No database lock held during the provider request. No channel/financial writes.
    try:
        with client_factory(max_get_attempts=1, timeout=5) as client:
            rows = client.account_booking_history_page(email=identity.email, after_id=cursor)
        if not isinstance(rows, list) or len(rows) > 100:
            raise HostawayResponseError("Invalid history page size.")
        ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
        if any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
            raise HostawayResponseError("Invalid history page identities.")
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().get(pk=user_id)
            job = BookingHistoryImport.objects.select_for_update().get(pk=job_id)
            if job.lease != lease or str(job.generation) != str(generation):
                return "obsolete"
            current_identity = identity_for(user)
            if not current_identity or not constant_time_compare(
                current_identity.digest, identity.digest
            ):
                raise HostawayResponseError("History identity changed.")
            if ids and ids[-1] in job.seen_cursors:
                raise HostawayResponseError("History cursor repeated.")
            for row in rows:
                if not matches(row.get("guestEmail"), row.get("phone"), identity):
                    continue  # Discard unrelated/missing/masked identities before projection.
                try:
                    # Keep a malformed matched record from undoing other safe results.
                    with transaction.atomic():
                        added, conflict = _store_match(user, identity, job, row)
                    job.added += added
                    job.conflicts += conflict
                except (HostawayResponseError, ValueError, TypeError, OverflowError):
                    job.skipped += 1
            job.pages += 1
            job.lease = job.locked_until = None
            if not rows:
                job.status = "complete"
                GuestBookingHistory.objects.filter(user=user).exclude(
                    last_seen_generation=job.generation
                ).update(is_visible=False)
            elif job.pages >= MAX_PAGES:
                job.status, job.error_code = "partial", "page_limit"
            else:
                job.cursor = ids[-1]
                job.seen_cursors = [*job.seen_cursors, job.cursor]
                job.status = "queued"
                transaction.on_commit(lambda: dispatch(job.pk, job.generation))
            job.save()
            return job.status
    except Exception:
        # Explicit partial result, never a false "no bookings" or leaked trace.
        BookingHistoryImport.objects.filter(pk=job_id, generation=generation, lease=lease).update(
            status="partial",
            error_code="source_unavailable",
            lease=None,
            locked_until=None,
            updated_at=timezone.now(),
        )
        return "partial"
