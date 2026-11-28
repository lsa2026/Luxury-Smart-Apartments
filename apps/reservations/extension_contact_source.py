"""Provider-backed contact selection and field provenance for new EXT drafts.

No writer, provider create, source20 RPC, or recipient-generation inference.
"""

from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_booking_phone
from apps.integrations.hostaway.reservation_validators import validate_reservation_response

from .models import StayExtension, StayExtensionChain
from .stay_extensions import billing_initial, guest_initial, read_source, source_snapshot

GUEST_FIELDS = (
    "guest_first_name",
    "guest_last_name",
    "guest_email",
    "guest_phone",
    "guest_language",
)
BILLING_FIELDS = (
    "billing_street1",
    "billing_city",
    "billing_state",
    "billing_postcode",
    "billing_country",
)
FIELDS = GUEST_FIELDS + BILLING_FIELDS
BILLING_PROVIDER_FIELDS = dict(
    zip(
        BILLING_FIELDS,
        ("guestAddress", "guestCity", "guestState", "guestZipCode", "guestCountry"),
        strict=True,
    )
)


def root_contact_source(selected_raw):
    """Reuse the selected root GET; a selected child requires a root GET.

    Membership comes from the stored chain, never a caller-supplied root ID.
    Closed children cannot make an older draft the source of current contact.
    """
    selected = source_snapshot(selected_raw)
    member = (
        StayExtension.objects.select_related("chain")
        .filter(reservation__hostaway_reservation_id=selected["id"])
        .first()
    )
    chain = (
        member.chain
        if member
        else StayExtensionChain.objects.filter(root_hostaway_reservation_id=selected["id"]).first()
    )
    if chain is None:
        return selected_raw
    raw = (
        selected_raw
        if selected["id"] == chain.root_hostaway_reservation_id
        else read_source(chain.root_hostaway_reservation_id)
    )
    if source_snapshot(raw) != chain.root_snapshot:
        raise ValueError("contact_root_changed")
    if selected["listing_map_id"] != chain.root_snapshot["listing_map_id"]:
        raise ValueError("contact_property_changed")
    return raw


def contact_values(raw):
    values = {**guest_initial(raw), **billing_initial(raw)}
    # Present provider fields, including explicit clears, outrank an older local
    # billing intent. Missing fields can still use the existing saved address.
    for field, provider in BILLING_PROVIDER_FIELDS.items():
        if provider in raw:
            values[field] = str(raw[provider] or "").strip()
    return {field: str(values.get(field) or "") for field in FIELDS}


def _canonical(field, value):
    value = str(value or "").strip()
    if field == "guest_phone" and value:
        try:
            return normalize_booking_phone(value)
        except InvalidPhoneNumber:
            return value
    if field in {"guest_email", "guest_language"}:
        return value.casefold()
    if field == "billing_country":
        return value.upper()
    return value


def field_provenance(values, *, user_id, root_id):
    """Only keyed hashes enter the signed form ticket, never guest/address text."""
    return {
        field: salted_hmac(
            "extension-contact-fields-1",
            f"{user_id}:{root_id}:{field}:{_canonical(field, values.get(field))}",
            algorithm="sha256",
        ).hexdigest()
        for field in FIELDS
    }


def merge_contact_fields(submitted, fresh_raw, *, baseline, user_id, root_id):
    if type(baseline) is not dict or set(baseline) != set(FIELDS):
        raise ValueError("contact_provenance_required")
    if fresh_raw.get("id") != root_id:
        raise ValueError("contact_root_mismatch")
    posted = {**submitted, **submitted.get("billing_address", {})}
    hashes = field_provenance(posted, user_id=user_id, root_id=root_id)
    fresh = contact_values(fresh_raw)
    merged = {
        field: fresh[field] if hashes[field] == baseline[field] else posted.get(field, "")
        for field in FIELDS
    }
    result = dict(submitted)
    result.update({field: merged[field] for field in GUEST_FIELDS})
    result["billing_address"] = {field: merged[field] for field in BILLING_FIELDS}
    return result


def provider_context(raw):
    """Allowlisted evidence from an already performed GET; no clock fallback."""
    item = validate_reservation_response({"status": "success", "result": raw})
    snapshot = source_snapshot(raw)
    guest = guest_initial(raw)
    timestamp_field = "updatedOn" if raw.get("updatedOn") else "latestActivityOn"
    timestamp_raw = raw.get(timestamp_field)
    timestamp_raw = timestamp_raw if isinstance(timestamp_raw, str) else None
    # The existing validator applies the server timezone to naive values. Do
    # not turn that assumption into provider chronology in the new wire.
    parsed = parse_datetime(timestamp_raw) if timestamp_raw else None
    timestamp = parsed.isoformat() if parsed and timezone.is_aware(parsed) else None
    return {
        "hostaway_reservation_id": item.reservation_id,
        "listing_map_id": item.listing_map_id,
        "status": snapshot["status"],
        "provider_status": item.status,
        "provider_updated_at": timestamp,
        "provider_updated_at_raw": timestamp_raw,
        "provider_updated_at_field": timestamp_field if timestamp_raw else None,
        "contact": guest,
    }
