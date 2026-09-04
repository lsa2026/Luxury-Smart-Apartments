"""The host's own listing policy, as Hostaway reports it.

The calendar already carries per-day stay rules and the availability service
applies them. These are the listing-level values that the calendar does not
express: the cancellation policy, the same-day rule with its lead time, and the
stay bounds that act as a fallback when a calendar day omits them.

A value Hostaway did not report is ``None``, and ``None`` never means "no
restriction" here: an unknown policy simply cannot block, and the caller still
has the live calendar underneath it.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from apps.properties.models import Property

ALLOWED = ""
MINIMUM_STAY_NOT_MET = "minimum_stay_not_met"
MAXIMUM_STAY_EXCEEDED = "maximum_stay_exceeded"
SAME_DAY_CHANGE_NOT_ALLOWED = "same_day_change_not_allowed"
ARRIVAL_LEAD_TIME_NOT_MET = "arrival_lead_time_not_met"

# Hostaway reports check-in as an hour of the day; 24 means midnight following.
_DEFAULT_CHECK_IN_HOUR = 15


def listing_timezone(property_obj: Property) -> ZoneInfo | None:
    """The listing's own timezone, or None when Hostaway did not report a valid one."""
    name = (property_obj.time_zone_name or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def check_in_datetime(property_obj: Property, check_in: date) -> datetime:
    """When the stay actually begins, in the listing's timezone where known.

    Using the real arrival moment rather than midnight keeps a cutoff from
    expiring hours early or late for a guest in another timezone.
    """
    zone = listing_timezone(property_obj) or timezone.get_current_timezone()
    hour = property_obj.check_in_time_start
    if hour is None or not 0 <= hour <= 24:
        hour = _DEFAULT_CHECK_IN_HOUR
    if hour == 24:
        return datetime.combine(check_in + timedelta(days=1), time(0), tzinfo=zone)
    return datetime.combine(check_in, time(hour), tzinfo=zone)


def policy_blocker(
    property_obj: Property | None,
    *,
    check_in: date,
    check_out: date,
    now: datetime | None = None,
) -> str:
    """Return the reason code the host's listing policy refuses this stay for."""
    if property_obj is None:
        return ALLOWED
    nights = (check_out - check_in).days
    if property_obj.min_nights and nights < property_obj.min_nights:
        return MINIMUM_STAY_NOT_MET
    if property_obj.max_nights and nights > property_obj.max_nights:
        return MAXIMUM_STAY_EXCEEDED

    moment = now or timezone.now()
    zone = listing_timezone(property_obj) or timezone.get_current_timezone()
    local_today = moment.astimezone(zone).date()
    if check_in > local_today:
        # Only an arrival today is governed by the same-day rule; the lead time
        # below covers how close to that arrival a change may still be made.
        return ALLOWED
    if property_obj.allow_same_day_booking is False:
        return SAME_DAY_CHANGE_NOT_ALLOWED
    lead_hours = property_obj.same_day_booking_lead_time_hours
    if lead_hours:
        arrival = check_in_datetime(property_obj, check_in)
        if arrival - moment < timedelta(hours=lead_hours):
            return ARRIVAL_LEAD_TIME_NOT_MET
    return ALLOWED
