"""Scheduled reward eligibility is not evidence of physical arrival."""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.reservations.models import Reservation

POLICY = "paid-checkin-1500-or-admin-v1"
TERMS_VERSION = "loyalty-public-20261016-v2"
TERMS_URL_VERSION = "2026-10-16-v2"
TERMS_REFERENCE = f"/ar/legal/rewards/{TERMS_URL_VERSION}/"
IDENTITY_TERMS_VERSION = "loyalty-public-20261017-v3"
IDENTITY_TERMS_URL_VERSION = "2026-10-17-v3"
IDENTITY_TERMS_REFERENCE = f"/ar/legal/rewards/{IDENTITY_TERMS_URL_VERSION}/"
SIGNED_TERMS_VERSION = "loyalty-public-20261017-v4"
SIGNED_TERMS_URL_VERSION = "2026-10-17-v4"
SIGNED_TERMS_REFERENCE = f"/ar/legal/rewards/{SIGNED_TERMS_URL_VERSION}/"


def policy_enabled():
    return bool(
        settings.LOYALTY_ENABLED
        and settings.LOYALTY_TERMS_APPROVED
        and (settings.LOYALTY_TERMS_VERSION, settings.LOYALTY_TERMS_REFERENCE)
        in {
            (TERMS_VERSION, TERMS_REFERENCE),
            (IDENTITY_TERMS_VERSION, IDENTITY_TERMS_REFERENCE),
            (SIGNED_TERMS_VERSION, SIGNED_TERMS_REFERENCE),
        }
    )


def scheduled_eligibility(booking):
    """Stable server evidence; a completed award survives later date/refund changes."""
    previous = booking.loyalty_state.get("scheduled_accrual", {})
    if previous.get("policy") == POLICY:
        at = parse_datetime(previous.get("at", ""))
        if at and timezone.is_aware(at) and at <= timezone.now():
            return previous
    if not policy_enabled() or booking.normalized_status not in Reservation.ACTIVE_STATUSES:
        return None
    from .site_adapter import stay_times

    start, end = stay_times(booking)
    if not start <= timezone.now() < end:
        return None
    return {
        "policy": POLICY,
        "kind": "scheduled_checkin",
        "at": start.isoformat(),
        "time_zone": booking.property.time_zone_name,
        "reference": f"scheduled-checkin:{booking.pk}:{start.isoformat()}",
    }


def effective_arrival(booking):
    """One operational start for rewards, invitations and stay settlement.

    Keep timed admission separate from physical reception evidence. An award's
    saved timed admission survives checkout/cancellation and never moves with
    a later change to dates. Merely viewing a booking does not write evidence.
    """
    manual = booking.loyalty_state.get("arrival")
    scheduled = scheduled_eligibility(booking)
    if manual and scheduled:
        manual_at = parse_datetime(manual.get("at", ""))
        scheduled_at = parse_datetime(scheduled["at"])
        if manual_at and timezone.is_aware(manual_at) and scheduled_at < manual_at:
            return scheduled
    return manual or scheduled


def accrue_due_stays():
    """Catch up current stays only; no retrospective sweep of finished bookings."""
    if not policy_enabled():
        return {"status": "disabled", "checked": 0, "settled": 0, "movements": 0}
    from apps.accounts.models import VerifiedPhone

    from .services import launch_at
    from .site_adapter import _safe_reconcile

    known_phones = VerifiedPhone.objects.filter(
        user__is_active=True, user__member__status="active"
    ).values("phone")

    day = timezone.now().date()
    bookings = (
        Reservation.objects.filter(
            Q(
                booking_intent__customer__is_active=True,
                booking_intent__customer__member__status="active",
            )
            | Q(
                booking_intent__customer__isnull=True, booking_intent__guest_phone__in=known_phones
            ),
            source_type=Reservation.SourceType.DIRECT_WEBSITE,
            normalized_status__in=Reservation.ACTIVE_STATUSES,
            created_at__gte=launch_at(),
            check_in__lte=day + timedelta(days=1),
            check_out__gte=day - timedelta(days=1),
        )
        .exclude(loyalty_state__has_key="arrival")
        .exclude(loyalty_state__has_key="settlement")
        .select_related("property")
        .order_by("created_at", "pk")
    )
    result = {"status": "completed", "checked": 0, "settled": 0, "movements": 0}
    for booking in bookings.iterator(chunk_size=100):
        result["checked"] += 1
        try:
            due = scheduled_eligibility(booking)
        except (ValidationError, ValueError, KeyError):
            due = None
        if not due:
            continue
        # This re-reads payment, membership, booking and time under the same
        # financial lock used by staff arrival, so simultaneous paths cannot double-post.
        outcome = _safe_reconcile(booking.pk) or {}
        result["settled"] += outcome.get("status") == "settled"
        result["movements"] += outcome.get("movements", 0)
    return result
