"""Read Hostaway stays; retain only exact phone hashes and public stay context."""

import hashlib
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import phonenumbers
from django.core.cache import cache
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayResponseError
from apps.integrations.hostaway.reservation_validators import normalize_hostaway_reservation_status
from apps.properties.models import Property
from apps.reservations.models import Reservation


def canonical_phone(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    value = unicodedata.normalize("NFKC", value)
    value = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value)
    if not re.fullmatch(r"[+0-9\s().-]+", value):
        return None
    digits = re.sub(r"[\s().-]", "", value)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif not digits.startswith("+"):
        digits = "+" + digits  # Accept only a valid complete international number.
    try:
        number = phonenumbers.parse(digits, None)
        if phonenumbers.is_valid_number(number):
            return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return None


def phone_key(phone):
    return hashlib.sha256(phone.encode()).hexdigest()


def project_stay(row, properties):
    phone = canonical_phone(row.get("phone"))
    if not phone:
        return None  # A partial/missing phone is not proof the sender has no booking.
    status = normalize_hostaway_reservation_status(row.get("status", ""))
    if status not in Reservation.ACTIVE_STATUSES:
        return None
    property_obj = properties.get(row.get("listingMapId"))
    if property_obj is None:
        return None
    check_in, check_out = (
        date.fromisoformat(row["arrivalDate"]),
        date.fromisoformat(row["departureDate"]),
    )
    if check_out <= check_in:
        raise HostawayResponseError("Invalid stay context dates.")
    zone = ZoneInfo(
        property_obj.time_zone_name
        or {"SA": "Asia/Riyadh", "MA": "Africa/Casablanca"}[property_obj.country_code.upper()]
    )
    arrival_hour = property_obj.display_check_in_hour
    departure_hour = property_obj.display_check_out_hour
    arrival_hour = arrival_hour if arrival_hour is not None else 15
    departure_hour = departure_hour if departure_hour is not None else 12
    start = datetime.combine(check_in, time.min, zone) + timedelta(hours=arrival_hour)
    end = datetime.combine(check_out, time.min, zone) + timedelta(hours=departure_hour)
    if end <= start:
        raise HostawayResponseError("Invalid stay context interval.")
    return {
        "phone_key": phone_key(phone),
        "property_slug": property_obj.slug,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
    }


def current_guest_context(phone, *, client_factory=HostawayClient):
    now = timezone.now()
    local_dates = [now.astimezone(ZoneInfo(z)).date() for z in ("Asia/Riyadh", "Africa/Casablanca")]
    earliest, latest = min(local_dates) - timedelta(days=1), max(local_dates) + timedelta(days=1)
    # One small, shared date-window read; never query Hostaway with a phone in its URL.
    key = f"sama-guest-context-v1:{earliest}:{latest}"
    document = cache.get(key)
    if document is None:
        properties = {}
        for p in Property.objects.filter(is_visible=True, hostaway_is_active=True):
            if not p.hostaway_listing_map_id:
                continue
            if p.hostaway_listing_map_id in properties:
                raise HostawayResponseError("Ambiguous property mapping.")
            properties[p.hostaway_listing_map_id] = p
        if not properties:
            raise HostawayResponseError("No verified stay-context property mapping.")
        projected, seen, cursor = [], set(), None
        with client_factory(max_get_attempts=1, timeout=4) as client:
            for _ in range(3):
                rows = client.guest_stay_page(earliest, latest, after_id=cursor)
                for row in rows:
                    identifier = row.get("id")
                    if type(identifier) is not int or identifier <= 0 or identifier in seen:
                        raise HostawayResponseError("Incomplete stay context pagination.")
                    seen.add(identifier)
                    stay = project_stay(row, properties)
                    if stay:
                        projected.append(stay)
                if len(rows) < 100:
                    break
                cursor = rows[-1]["id"]
            else:
                raise HostawayResponseError("Stay context pagination exceeded its bound.")
        document = {"checked_at": timezone.now().isoformat(), "stays": projected}
        cache.set(key, document, timeout=30)
    now = timezone.now()
    matches = []
    for row in document["stays"]:
        if row["phone_key"] != phone_key(phone):
            continue
        start, end = (
            datetime.fromisoformat(row["starts_at"]),
            datetime.fromisoformat(row["ends_at"]),
        )
        phase = "current" if start <= now < end else "before_arrival" if now < start else "departed"
        matches.append({**{k: v for k, v in row.items() if k != "phone_key"}, "phase": phase})
    if len(matches) > 6:
        raise HostawayResponseError("Ambiguous stay context.")
    return {
        "code": "guest_context",
        "source": "hostaway_read_only",
        "match_key": phone_key(phone),
        "checked_at": document["checked_at"],
        "stays": matches,
    }
