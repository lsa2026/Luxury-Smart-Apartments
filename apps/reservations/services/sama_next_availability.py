"""Bounded, read-only next-stay search using the existing calendar rules."""

import time
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayResponseError

from .availability import AVAILABLE, CALENDAR_INCOMPLETE, evaluate_calendar, resolve_day_inventory


def next_availability(property_obj, check_in, check_out, guests, *, client_factory=HostawayClient):
    zone = ZoneInfo(
        property_obj.time_zone_name
        or ("Africa/Casablanca" if property_obj.country_code.upper() == "MA" else "Asia/Riyadh")
    )
    today = timezone.now().astimezone(zone).date()
    start = date.fromisoformat(check_in) if check_in else today
    end = date.fromisoformat(check_out) if check_out else None
    nights = (end - start).days if end else None
    if (
        start < today
        or (start - today).days > 730
        or (check_out and not check_in)
        or (nights is not None and not 1 <= nights <= 366)
    ):
        raise ValueError("Invalid calendar enquiry dates.")
    if guests is not None and (type(guests) is not int or not 1 <= guests <= 20):
        raise ValueError("Invalid calendar guest count.")
    if guests and property_obj.person_capacity and guests > property_obj.person_capacity:
        return {
            "code": "next_availability",
            "source": "hostaway_read_only",
            "property_slug": property_obj.slug,
            "request": {"check_in": check_in, "check_out": check_out, "guests": guests},
            "status": "capacity_exceeded",
            "next_stay": None,
            "checked_at": timezone.now().isoformat(),
        }
    horizon = start + timedelta(days=730)
    last_day = horizon + timedelta(days=nights or 366)
    days, cursor, requested_reason = {}, start, None
    deadline = time.monotonic() + 22

    def check_deadline():
        if time.monotonic() > deadline:
            raise HostawayResponseError("Next-availability time bound exceeded.")

    with client_factory(max_get_attempts=1, timeout=4) as client:

        def ensure_coverage(required):
            nonlocal cursor, requested_reason
            while required not in days:
                check_deadline()
                if required > last_day:
                    raise HostawayResponseError("Calendar search bound exceeded.")
                window_end = max(
                    cursor + timedelta(days=1), min(cursor + timedelta(days=90), last_day)
                )
                document = client.get_listing_calendar(
                    property_obj.hostaway_listing_id,
                    start_date=cursor,
                    end_date=window_end,
                    include_resources=False,
                )
                check_deadline()
                expected = {
                    cursor + timedelta(days=i) for i in range((window_end - cursor).days + 1)
                }
                if {d.date for d in document.days} != expected or len(document.days) != len(
                    expected
                ):
                    raise HostawayResponseError("Incomplete next-availability calendar.")
                days.update({d.date: d for d in document.days})
                cursor = window_end + timedelta(days=1)
                if end and end in days and requested_reason is None:
                    requested_reason = evaluate_calendar(
                        tuple(days.values()), check_in=start, check_out=end
                    )

        if end:
            ensure_coverage(end)
        candidate = start
        while candidate <= horizon:
            check_deadline()
            ensure_coverage(candidate)
            arrival = days[candidate]
            inventory = resolve_day_inventory(arrival)
            if not inventory.is_available or arrival.closed_on_arrival is True:
                candidate += timedelta(days=1)
                continue
            minimum = nights if nights is not None else max(1, arrival.minimum_stay or 1)
            maximum = nights if nights is not None else min(366, arrival.maximum_stay or 366)
            for duration in range(minimum, maximum + 1):
                check_deadline()
                if not 1 <= duration <= 366:
                    continue
                departure = candidate + timedelta(days=duration)
                ensure_coverage(departure)
                reason = evaluate_calendar(
                    tuple(days.values()), check_in=candidate, check_out=departure
                )
                if reason == CALENDAR_INCOMPLETE:
                    raise HostawayResponseError("Incomplete stay calendar.")
                if reason == AVAILABLE:
                    check_deadline()
                    return {
                        "code": "next_availability",
                        "source": "hostaway_read_only",
                        "property_slug": property_obj.slug,
                        "request": {"check_in": check_in, "check_out": check_out, "guests": guests},
                        "status": "verified_calendar",
                        "requested_reason": requested_reason,
                        "next_stay": {
                            "check_in": candidate.isoformat(),
                            "check_out": departure.isoformat(),
                            "nights": duration,
                        },
                        "checked_at": timezone.now().isoformat(),
                        "searched_through": candidate.isoformat(),
                    }
                if reason in (
                    "unavailable_dates",
                    "inventory_conflict",
                    "closed_on_arrival",
                    "maximum_stay_exceeded",
                ):
                    break
            candidate += timedelta(days=1)
    return {
        "code": "next_availability",
        "source": "hostaway_read_only",
        "property_slug": property_obj.slug,
        "request": {"check_in": check_in, "check_out": check_out, "guests": guests},
        "status": "no_calendar_stay_in_window",
        "requested_reason": requested_reason,
        "next_stay": None,
        "checked_at": timezone.now().isoformat(),
        "searched_through": horizon.isoformat(),
    }
