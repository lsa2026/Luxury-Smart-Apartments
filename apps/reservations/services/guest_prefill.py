"""Read saved guest details for an existing operations session; never create a stay.

Each candidate is one matching source. Addresses are not combined across people,
and recovering contact data never verifies a phone, signs terms or authorizes money.
"""

import re

import phonenumbers
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q

from apps.accounts.models import CustomerProfile
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_comparison_phone
from apps.integrations.hostaway.client import HostawayClient
from apps.payments.countries import normalize_country_code
from apps.reservations.booking_forms import BillingAddressForm, _clean_text
from apps.reservations.models import DayUseStay, MorningStay, Reservation

from .guest_classification import contact_keys

ADDRESS_FIELDS = (
    "billing_street1",
    "billing_city",
    "billing_state",
    "billing_country",
    "billing_postcode",
)
CONTACT_FIELDS = (
    "guest_first_name",
    "guest_last_name",
    "guest_name",
    "guest_email",
    "guest_phone",
    "guest_phone_country",
    "guest_language",
)
PLACEHOLDERS = {"not provided", "unknown", "n/a", "none", "null", "-"}


def text(value):
    if not isinstance(value, str) or len(value) > 500:
        return ""
    result = "".join(c for c in value if c.isprintable() or c == "\n").strip()
    return "" if result.casefold() in PLACEHOLDERS else result


def phone(value):
    try:
        return normalize_comparison_phone(text(value))
    except InvalidPhoneNumber:
        return ""


def phone_variants(value):
    if not value:
        return []
    parsed = phonenumbers.parse(value, None)
    variants = {value, value[1:], "00" + value[1:]}
    for style in (
        phonenumbers.PhoneNumberFormat.INTERNATIONAL,
        phonenumbers.PhoneNumberFormat.NATIONAL,
    ):
        formatted = phonenumbers.format_number(parsed, style)
        variants.update((formatted, re.sub(r"\D", "", formatted)))
    return sorted(variants)


def contact_query(email_field, phone_field, email, number):
    query = Q()
    if email:
        query &= Q(**{email_field + "__iexact": email})
    if number:
        query &= Q(**{phone_field + "__in": phone_variants(number)})
    return query


def matches(fields, identity):
    actual = contact_keys(fields.get("guest_email"), fields.get("guest_phone"))
    # BOTH supplied identifiers must belong to this same record. Classification
    # uses OR for reporting; it is intentionally not authority for restoring PII.
    return any(identity) and all(
        not wanted or wanted == found for wanted, found in zip(identity, actual, strict=True)
    )


def candidate(key, label, fields, address, *, address_kind, saved_at, identity):
    if not matches(fields, identity):
        return None
    address = address if isinstance(address, dict) else {}
    names = text(fields.get("guest_name")).split(maxsplit=1)
    first = _clean_text(text(fields.get("guest_first_name"))) or (names[0] if names else "")
    last = _clean_text(text(fields.get("guest_last_name"))) or (names[1] if len(names) > 1 else "")
    number = phone(fields.get("guest_phone"))
    country = (
        phonenumbers.region_code_for_number(phonenumbers.parse(number, None)) if number else ""
    )
    values = {
        "guest_first_name": first,
        "guest_last_name": last,
        "guest_name": (first + " " + last).strip(),
        "guest_email": text(fields.get("guest_email")).casefold(),
        "guest_phone": number,
        "guest_phone_country": country or "",
        "guest_language": fields.get("guest_language")
        if fields.get("guest_language") in {"ar", "en", "fr"}
        else "",
        **{name: text(address.get(name)) for name in ADDRESS_FIELDS},
    }
    try:
        values["billing_country"] = normalize_country_code(values["billing_country"])
    except ValueError:
        values["billing_country"] = ""
    billing = {name: values[name] for name in ADDRESS_FIELDS}
    return {
        "source_ref": key,
        "source_label": label,
        "saved_at": str(saved_at or ""),
        "fields": values,
        "address_kind": address_kind,
        "address_available": any(billing.values()),
        "address_complete": BillingAddressForm(billing).is_valid(),
        "review_required": True,
    }


def from_booking(booking, identity):
    intent = booking.booking_intent
    return candidate(
        "booking:" + str(booking.pk),
        "حجز سابق في الموقع · " + str(booking.hostaway_reservation_id),
        {
            "guest_first_name": intent.guest_first_name,
            "guest_last_name": intent.guest_last_name,
            "guest_email": intent.guest_email,
            "guest_phone": intent.guest_phone,
            "guest_language": intent.language,
        },
        {name: getattr(intent, name, "") for name in ADDRESS_FIELDS},
        address_kind="billing",
        saved_at=booking.check_out,
        identity=identity,
    )


def directory_references(snapshot, identity):
    if not isinstance(snapshot, dict):
        return []
    email, number = identity
    found = []
    for row in snapshot.get("history", []):
        if not isinstance(row, dict):
            continue
        if all(
            not value or row.get(key) == value
            for key, value in (("email_key", email), ("phone_key", number))
        ):
            ref = row.get("id")
            if type(ref) is int and ref > 0:
                found.append((str(row.get("check_out") or ""), ref))
    if found:
        return list(dict.fromkeys(ref for _, ref in sorted(found, reverse=True)))[:5]
    sets = []
    for kind, key in (("email", email), ("phone", number)):
        if key:
            sets.append(
                {ref for ref in snapshot.get(kind, {}).get(key, []) if type(ref) is int and ref > 0}
            )
    return sorted(set.intersection(*sets), reverse=True)[:5] if sets else []


def lookup(email, number, *, snapshot=None, client=None):
    """Local reads first; at most ONE existing Hostaway reservation GET. No scan."""
    email, number = email.strip().casefold(), phone(number)
    identity = contact_keys(email, number)
    if not any(identity):
        return {
            "status": "not_found",
            "candidates": [],
            "history_available": False,
            "remote_unavailable": False,
        }
    results = []
    bookings = (
        Reservation.objects.filter(
            is_test=False,
            booking_intent__isnull=False,
            hostaway_reservation_id__gt=0,
            normalized_status__in=Reservation.ACTIVE_STATUSES,
        )
        .filter(
            contact_query(
                "booking_intent__guest_email", "booking_intent__guest_phone", email, number
            )
        )
        .select_related("booking_intent")
        .order_by("-check_out", "-created_at")[:5]
    )
    for booking in bookings:
        result = from_booking(booking, identity)
        if result:
            results.append(result)
    for model, label, prefix in (
        (MorningStay, "إقامة صباحية سابقة", "morning"),
        (DayUseStay, "إقامة نهارية سابقة", "dayuse"),
    ):
        rows = (
            model.objects.filter(environment="production", status__in={"paid", "open", "expired"})
            .filter(contact_query("guest_email", "guest_phone", email, number))
            .order_by("-ends_at")[:3]
        )
        for stay in rows:
            from apps.reservations import day_use08_communications, morning07_communications

            comms = morning07_communications if model is MorningStay else day_use08_communications
            if not comms.verified_booking(stay):
                continue
            result = candidate(
                prefix + ":" + str(stay.pk),
                label,
                {
                    "guest_name": stay.guest_name,
                    "guest_phone": stay.guest_phone,
                    "guest_email": stay.guest_email,
                    "guest_language": stay.guest_language,
                },
                stay.billing_address or {},
                address_kind="billing",
                saved_at=stay.ends_at.date(),
                identity=identity,
            )
            if result:
                results.append(result)
    query = Q(user__is_active=True)
    if email:
        query &= Q(user__email__iexact=email)
    if number:
        query &= Q(phone__in=phone_variants(number)) | Q(user__verifiedphone__phone=number)
    profiles = (
        CustomerProfile.objects.filter(query)
        .select_related("user", "user__verifiedphone")
        .order_by("-updated_at")[:3]
    )
    for profile in profiles:
        try:
            verified = profile.user.verifiedphone.phone
        except ObjectDoesNotExist:
            verified = ""
        result = candidate(
            "profile:" + str(profile.user_id),
            "الملف المحفوظ للضيف",
            {
                "guest_first_name": profile.user.first_name,
                "guest_last_name": profile.user.last_name,
                "guest_email": profile.user.email,
                "guest_phone": phone(profile.phone) or verified,
                "guest_language": profile.preferred_language,
            },
            {
                "billing_street1": profile.residence_address_line1,
                "billing_city": profile.residence_city,
                "billing_state": profile.residence_region,
                "billing_country": profile.residence_country,
                "billing_postcode": profile.residence_postal_code,
            },
            address_kind="residence",
            saved_at=profile.updated_at.date(),
            identity=identity,
        )
        if result:
            results.append(result)
    remote_unavailable = False
    refs = directory_references(snapshot, identity)
    # A complete local billing address already solves the lookup immediately.
    if refs and not any(r["address_kind"] == "billing" and r["address_complete"] for r in results):
        try:
            if client is None:
                with HostawayClient(max_get_attempts=1, timeout=5) as remote:
                    payload = remote.get_reservation_invoice_data(refs[0])
            else:
                payload = client.get_reservation_invoice_data(refs[0])
            raw = (
                payload.get("result")
                if isinstance(payload, dict) and payload.get("status") == "success"
                else None
            )
            if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] != refs[0]:
                raise ValueError("source_mismatch")
            result = candidate(
                "hostaway:" + str(raw["id"]),
                "حجز سابق في هوستاوي · " + str(raw["id"]),
                {
                    "guest_first_name": raw.get("guestFirstName"),
                    "guest_last_name": raw.get("guestLastName"),
                    "guest_name": raw.get("guestName"),
                    "guest_email": raw.get("guestEmail"),
                    "guest_phone": raw.get("phone"),
                    "guest_language": text(raw.get("guestLocale")).split("-", 1)[0].casefold(),
                },
                dict(
                    zip(
                        ADDRESS_FIELDS,
                        (
                            raw.get("guestAddress"),
                            raw.get("guestCity"),
                            raw.get("guestState"),
                            raw.get("guestCountry"),
                            raw.get("guestZipCode"),
                        ),
                        strict=True,
                    )
                ),
                address_kind="contact",
                saved_at=raw.get("departureDate"),
                identity=identity,
            )
            if result:
                results.append(result)
        except Exception:
            remote_unavailable = True  # Never log provider body/exception or raw contact details.
    results.sort(
        key=lambda r: (r["address_kind"] == "billing" and r["address_complete"], r["saved_at"]),
        reverse=True,
    )
    return {
        "status": "found" if results else "unavailable" if remote_unavailable else "not_found",
        "candidates": results[:5],
        "history_available": snapshot is not None,
        "remote_unavailable": remote_unavailable,
    }
