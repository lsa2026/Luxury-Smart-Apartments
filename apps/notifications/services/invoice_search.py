"""Transient exact-phone owner lookup; no imported guests or reservation writes."""

import phonenumbers
from django.core import signing
from django.core.exceptions import ValidationError
from django.utils.dateparse import parse_date

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.integrations.hostaway.exceptions import HostawayResponseError
from apps.notifications.invoice_forms import plain
from apps.notifications.invoice_phone import normalize_invoice_phone
from apps.properties.models import Property

SALT = "guest-invoice-phone-search-v1"


def search_token(*, actor, phone, cursor=None, reservation_id=None):
    return signing.dumps(
        {"actor": str(actor.pk), "phone": phone, "cursor": cursor, "id": reservation_id},
        salt=SALT,
    )


def read_search_token(value, actor):
    try:
        if len(value) > 1024:
            raise ValueError
        data = signing.loads(value, salt=SALT, max_age=900)
        if data["actor"] != str(actor.pk):
            raise ValueError
        if normalize_phone_number(data["phone"]) != data["phone"]:
            raise ValueError
        for key in ("cursor", "id"):
            number = data.get(key)
            if number is not None and (
                type(number) is not int or not 0 < number <= 9223372036854775807
            ):
                raise ValueError
        return data
    except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
        raise ValidationError("انتهت صلاحية نتائج البحث أو تغيّرت؛ أعد البحث برقم الجوال.") from exc


def stored_phone(row):
    value = plain(row.get("phone"))
    if not value:
        return ""
    # Clean copied Unicode formatting on provider values just as on owner input.
    try:
        return normalize_invoice_phone(value)
    except InvalidPhoneNumber:
        country = plain(row.get("guestCountry")).upper()
        if country not in phonenumbers.SUPPORTED_REGIONS:
            return ""
        try:
            return normalize_invoice_phone(value, country=country)
        except InvalidPhoneNumber:
            return ""


def find_invoice_bookings(*, client, phone, after_id=None):
    """One bounded 50-row request per click; no caching PII or nested slow requests."""
    properties = list(Property.objects.all())
    candidates = []
    seen = set()
    cursor = after_id
    for _ in range(1):
        rows = client.invoice_search_page(after_id=cursor)
        for row in rows:
            reservation_id = row["id"]
            if reservation_id in seen or reservation_id == after_id:
                raise HostawayResponseError("Invoice search repeated a reservation.")
            seen.add(reservation_id)
            if stored_phone(row) != phone:
                continue
            if plain(row.get("status")).casefold() not in {
                "new",
                "confirmed",
                "modified",
                "awaitingpayment",
            }:
                continue
            listing_id = row.get("listingMapId")
            matching = [
                p
                for p in properties
                if listing_id in (p.hostaway_listing_id, p.hostaway_listing_map_id)
            ]
            if listing_id is None or len(matching) != 1:
                continue
            try:
                arrival = parse_date(plain(row.get("arrivalDate")))
                departure = parse_date(plain(row.get("departureDate")))
            except ValueError:
                continue
            if not arrival or not departure or departure <= arrival:
                continue
            platform = plain(row.get("channelName"))[:80]
            reference = plain(
                (row.get("confirmationCode") if "airbnb" in platform.casefold() else "")
                or row.get("channelReservationId")
            )[:100]
            from apps.notifications.services.invoices import re_internal_reference

            if reference == str(reservation_id) or re_internal_reference(reference):
                reference = ""
            candidates.append(
                {
                    "id": reservation_id,
                    "guest_name": plain(row.get("guestName"))[:160],
                    "property_name": matching[0].name_ar or matching[0].name_en,
                    "platform": platform or "حجز مباشر",
                    "reference": reference,
                    "check_in": arrival.isoformat(),
                    "check_out": departure.isoformat(),
                }
            )
        if len(rows) < 50:
            return candidates, None
        cursor = rows[-1]["id"]
    return candidates, cursor
