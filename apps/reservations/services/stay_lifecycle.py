"""Shared cancellation cutoff, based on the property's actual checkout instant."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from ..models import Reservation

STAY_COMPLETED = "reservation_stay_completed"


def checkout_datetime(reservation: Reservation) -> datetime:
    """Display checkout hour, then imported hour, then noon in the listing zone.

    An hour of 24 means midnight following the checkout date. Missing or invalid
    listing zones use Casablanca for Morocco and Riyadh for other properties;
    the viewer's current timezone must never change the cancellation cutoff.
    """
    property_obj = reservation.property
    country = (getattr(property_obj, "country_code", "") or "").strip().upper()
    fallback = "Africa/Casablanca" if country == "MA" else "Asia/Riyadh"
    name = (getattr(property_obj, "time_zone_name", "") or "").strip()
    try:
        zone = ZoneInfo(name or fallback)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(fallback)
    hour = next(
        value
        for value in (
            getattr(property_obj, "display_check_out_hour", None),
            getattr(property_obj, "check_out_time", None),
            12,
        )
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 24
    )
    return datetime.combine(reservation.check_out, time.min, zone) + timedelta(hours=hour)


def cancellation_blocker(reservation: Reservation, *, now: datetime | None = None) -> str:
    """Return STAY_COMPLETED for a completed stay; otherwise return an empty string.

    Booking-intent completion and paid UI labels do not mean the stay ended.
    Orphaned unconfirmed direct records have no external stay to cancel:
    keep their existing refund/test cleanup workflow, even after their dates.
    """
    if any(
        (value or "").strip().casefold() == "completed"
        for value in (reservation.normalized_status, reservation.hostaway_status)
    ):
        return STAY_COMPLETED
    if (
        reservation.source_type == Reservation.SourceType.DIRECT_WEBSITE
        and reservation.normalized_status
        not in Reservation.ACTIVE_STATUSES | Reservation.CLOSED_STATUSES
        and reservation.hostaway_reservation_id is None
    ):
        return ""
    return STAY_COMPLETED if (now or timezone.now()) >= checkout_datetime(reservation) else ""
