"""Bounded inventory advice; never a quote, reservation or physical-departure proof."""

import hashlib
import json
import time as clock
from collections import Counter
from dataclasses import asdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.integrations.hostaway.reservation_validators import normalize_hostaway_reservation_status
from apps.properties.models import Property
from apps.reservations.day_use_stays import (
    DayUseStayError,
    active_stays,
    local_inflight_conflict,
    property_zone,
    verify_window,
)
from apps.reservations.models import Reservation
from apps.reservations.morning_stays import active_stays as morning_active

CONTRACT = "site01-sma-dayuse-availability-1"
MAX_AGE = 15
MAX_PROPERTIES = 30
ZONE = ZoneInfo("Asia/Riyadh")
KNOWN_EXCLUSIONS = {
    "local_inflight",
    "calendar_blocked",
    "calendar_unavailable",
    "inventory_stay_restriction",
    "inventory_boundary_restriction",
    "morning_conflict",
    "channel_stay_or_departure",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        ).encode()
    ).hexdigest()


class SourceUnavailable(ValueError):
    pass


def occupancy_projection(row):
    safe = {
        name: row.get(name) if type(row.get(name)) is int else None
        for name in ("id", "listingMapId")
    }
    status = row.get("status")
    safe["status"] = (
        normalize_hostaway_reservation_status(status)
        if isinstance(status, str) and len(status) <= 64
        else "unknown"
    )
    for name in ("arrivalDate", "departureDate"):
        value = row.get(name)
        try:
            safe[name] = (
                datetime.fromisoformat(value).date().isoformat()
                if isinstance(value, str) and len(value) <= 32
                else None
            )
        except ValueError:
            safe[name] = None
    return safe


class Reader:
    """Cache only validated calendar DTOs and five-field occupancy projections."""

    def __init__(self, remote):
        self.remote = remote
        self.deadline = clock.monotonic() + 12
        self.times, self.evidence = [], []

    def read(self, key, load, project):
        if clock.monotonic() >= self.deadline:
            raise SourceUnavailable
        key = "sama-dayuse-read-1:" + digest(key)
        found = cache.get(key)
        now = timezone.now()
        if not isinstance(found, dict) or not 0 <= (now - found["at"]).total_seconds() < MAX_AGE:
            value = load()
            value, evidence = project(value)
            found = {"at": timezone.now(), "value": value, "digest": digest(evidence)}
            cache.set(key, found, timeout=MAX_AGE)
        if clock.monotonic() >= self.deadline:
            raise SourceUnavailable
        self.times.append(found["at"])
        self.evidence.append(found["digest"])
        return found["value"]

    def get_listing_calendar(self, listing_id, **kwargs):
        return self.read(
            ["calendar", listing_id, kwargs],
            lambda: self.remote.get_listing_calendar(listing_id, **kwargs),
            lambda value: (value, [asdict(day) for day in value.days]),
        )

    def guest_stay_page(self, start, end, *, after_id=None):
        def project(rows):
            if (
                not isinstance(rows, list)
                or len(rows) > 100
                or any(not isinstance(row, dict) for row in rows)
            ):
                raise SourceUnavailable
            safe = [occupancy_projection(row) for row in rows]
            return safe, safe

        return self.read(
            ["occupancy", start, end, after_id],
            lambda: self.remote.guest_stay_page(start, end, after_id=after_id),
            project,
        )


def local_reason(property_obj, start, end):
    # A scheduled checkout today remains excluded even after its contractual hour;
    # neither a sent reminder nor the clock is evidence of actual departure.
    day = start.date()
    if Reservation.objects.filter(
        property=property_obj,
        is_test=False,
        check_in__lte=day,
        check_out__gte=day,
        normalized_status__in=[
            *Reservation.ACTIVE_STATUSES,
            "awaiting_payment",
            "pending",
            "creating",
            "create_unknown",
            "create_pending",
            "ready_for_hostaway",
            "unknown",
            "sync_pending",
        ],
    ).exists():
        return "local_stay_or_departure"
    if local_inflight_conflict(property_obj, start, end):
        return "local_inflight"
    # This is an inventory-day preselection, not a requested late checkout.
    # The booking path applies its exact end-time turnover buffer separately.
    if active_stays(property_obj, start, end).exists():
        return "day_use_conflict"
    zone = property_zone(property_obj)
    if morning_active(
        property_obj,
        datetime.combine(day - timedelta(days=1), time(15), zone),
        datetime.combine(day + timedelta(days=1), time(12), zone),
    ).exists():
        return "morning_conflict"
    return None


def availability(*, client_factory=None):
    client_factory = client_factory or HostawayClient
    now = timezone.now()
    local = now.astimezone(ZONE)
    start = datetime.combine(local.date(), time.min, ZONE)
    noon = datetime.combine(local.date(), time(12), ZONE)
    base = {
        "contract_version": CONTRACT,
        "code": "day_use_availability",
        "timezone": ZONE.key,
        "local_date": local.date().isoformat(),
        "evaluated_at": now.isoformat(),
        "window": {"starts_at": start.isoformat(), "ends_at": noon.isoformat()},
        "max_source_age_seconds": MAX_AGE,
        "booking_authorized": False,
        "physical_readiness_verified": False,
        "source_checked_at": None,
        "properties": [],
    }
    if local >= noon:
        return {
            **base,
            "status": "window_closed",
            "complete": True,
            "offering_allowed": False,
            "expires_at": noon.isoformat(),
            "source_revision": None,
            "coverage": {"total": 0, "evaluated": 0, "unknown": 0},
        }, 200
    if not settings.DAY_USE08_ENABLED or settings.HYPERPAY_ENVIRONMENT != "production":
        return {
            **base,
            "status": "service_unavailable",
            "complete": False,
            "offering_allowed": False,
            "expires_at": now.isoformat(),
            "source_revision": None,
            "coverage": {"total": None, "evaluated": 0, "unknown": None},
        }, 503
    properties = list(Property.objects.order_by("sort_order", "id")[: MAX_PROPERTIES + 1])
    if not properties or len(properties) > MAX_PROPERTIES:
        raise SourceUnavailable
    mappings = Counter(
        p.hostaway_listing_map_id for p in properties if p.is_visible and p.hostaway_is_active
    )
    rows, expiries, windows = [], [noon], {}
    with client_factory(max_get_attempts=1, timeout=3) as remote:
        reader = Reader(remote)
        for property_obj in properties:
            row = {
                "property_slug": property_obj.slug,
                "property_id": str(property_obj.pk),
                "listing_map_id": property_obj.hostaway_listing_map_id,
                "names": {
                    lang: getattr(property_obj, "name_" + lang) for lang in ("ar", "en", "fr")
                },
                "city": property_obj.city,
                "country_code": property_obj.country_code,
                "capacity": property_obj.person_capacity,
                "booking_url": None,
                "eligible": None,
                "reason_codes": [],
                "timezone": None,
                "local_date": None,
                "same_day_end_at": None,
            }
            reason = None
            evidence_before = len(reader.evidence)
            if not property_obj.is_visible or not property_obj.hostaway_is_active:
                row.update(eligible=False, reason_codes=["property_unavailable"])
                row["source_revision"] = digest(row)
                rows.append(row)
                continue
            try:
                zone = property_zone(property_obj)
                day = timezone.now().astimezone(zone).date()
                beginning = datetime.combine(day, time.min, zone)
                ending = beginning + timedelta(days=1) - timedelta(microseconds=1)
                row.update(
                    timezone=zone.key,
                    local_date=day.isoformat(),
                    same_day_end_at=ending.isoformat(),
                )
                expiries.append(beginning + timedelta(days=1))
                windows[property_obj.pk] = (beginning, ending)
                if (
                    not property_obj.hostaway_listing_map_id
                    or mappings[property_obj.hostaway_listing_map_id] != 1
                ):
                    raise SourceUnavailable
                else:
                    reason = local_reason(property_obj, beginning, ending)
                    if not reason:
                        verify_window(
                            property_obj,
                            beginning,
                            ending,
                            client=reader,
                            allow_started=True,
                            include_pricing=False,
                        )
                        # Read local admissions again after the remote read, even on cache hits.
                        reason = local_reason(property_obj, beginning, ending)
                row["eligible"] = reason is None
            except DayUseStayError as exc:
                reason = exc.code
                row["eligible"] = False if reason in KNOWN_EXCLUSIONS else None
            except (HostawayError, SourceUnavailable):
                reason = "source_unverified"
            row["reason_codes"] = [reason] if reason else []
            row["source_revision"] = digest([row, reader.evidence[evidence_before:]])
            rows.append(row)
        checked = min(reader.times, default=timezone.now())
    # A booking admitted for an earlier property while a later remote read was
    # in progress must be reflected in this response too, not just on the next GET.
    for row, property_obj in zip(rows, properties, strict=True):
        if row["eligible"] is not True:
            continue
        current_mapping = Property.objects.filter(
            pk=property_obj.pk,
            is_visible=True,
            hostaway_is_active=True,
            hostaway_listing_id=property_obj.hostaway_listing_id,
            hostaway_listing_map_id=property_obj.hostaway_listing_map_id,
            time_zone_name=property_obj.time_zone_name,
            country_code=property_obj.country_code,
            person_capacity=property_obj.person_capacity,
        ).exists()
        reason = (
            local_reason(property_obj, *windows[property_obj.pk])
            if current_mapping
            else "source_metadata_changed"
        )
        if reason:
            previous = row.pop("source_revision")
            row.update(eligible=False if current_mapping else None, reason_codes=[reason])
            row["source_revision"] = digest([row, previous])
    finished = timezone.now()
    expires = min([checked + timedelta(seconds=MAX_AGE), *expiries])
    if finished >= expires or finished.astimezone(ZONE).date() != local.date():
        raise SourceUnavailable
    unknown = sum(row["eligible"] is None for row in rows)
    return {
        **base,
        "evaluated_at": finished.isoformat(),
        "source_checked_at": checked.isoformat(),
        "expires_at": expires.isoformat(),
        "properties": rows,
        "status": "verified" if not unknown else "source_unavailable",
        "complete": not unknown,
        "offering_allowed": not unknown,
        "source_revision": digest([CONTRACT, local.date(), rows]),
        "coverage": {"total": len(properties), "evaluated": len(rows), "unknown": unknown},
    }, 503 if unknown else 200
