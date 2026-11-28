"""Read-only compatibility for an authenticated current Source20 row; no ingestion.

The caller must obtain the row through the existing authenticated Source20
boundary and retain the returned contact reference in the canonical current
version. This module grants no source/send/payment authority, makes no provider
calls or database writes, and is not wired to a producer or existing payment URL.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number

CONTRACT = "site01-extension-contact-compat-1"
ROW_KEYS = {
    "row_id",
    "previous_row_id",
    "reservation_key",
    "scenario",
    "revision",
    "event_key",
    "source_updated_at",
    "listing_map_id",
    "version_status",
    "change_type",
    "recipient_generation",
    "contact",
}
CONTACT_KEYS = {"first_name", "last_name", "email", "phone", "language", "address"}
ADDRESS_LIMITS = {
    "billing_street1": 151,
    "billing_city": 80,
    "billing_state": 80,
    "billing_country": 2,
    "billing_postcode": 16,
}


class ContactMismatch(ValueError):
    """Safe machine codes only; never include the supplied contact packet."""


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ContactMismatch("source_uuid_invalid") from None
    return value


def _text(value, limit, *, required=False):
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(ord(c) < 32 for c in value)
        or (required and not value.strip())
    ):
        raise ContactMismatch("source_text_invalid")
    return value.strip()


def _phone(value):
    try:
        return normalize_phone_number(_text(value, 64, required=True))
    except (ValueError, InvalidPhoneNumber):
        raise ContactMismatch("source_phone_invalid") from None


@dataclass(frozen=True)
class ContactProjection:
    chain_id: str
    row_id: str
    reservation_key: str
    source_revision: int
    source_updated_at: datetime
    recipient_generation: int
    binding_sha256: str
    # Canonical bytes avoid a caller mutating a returned dict after validation.
    contact_json: str = field(repr=False)

    def contact(self):
        return json.loads(self.contact_json)


def project_current_contact(
    packet,
    *,
    chain_id,
    listing_map_id,
    scenario,
    member_keys,
    baseline_phone,
    baseline_generation=0,
    previous=None,
):
    """Project an explicit correction, independently of child active/cancel state.

    Source20 owns persistence/CAS of the current contact projection and the old
    recipient history. No age/cutover check or send decision belongs here.
    ``previous`` is that retained contact projection, never a newly reselected
    draft after a child cancellation. Baseline is used once, before adoption.
    """
    if (
        not isinstance(packet, dict)
        or set(packet) != {"contract", "chain_id", "current"}
        or packet["contract"] != CONTRACT
        or packet["chain_id"] != str(chain_id)
    ):
        raise ContactMismatch("contact_contract_mismatch")
    chain_id = _uuid(str(chain_id))
    row = packet["current"]
    if not isinstance(row, dict) or set(row) != ROW_KEYS:
        raise ContactMismatch("current_row_shape")
    row_id = _uuid(row["row_id"])
    if row["previous_row_id"] is not None:
        _uuid(row["previous_row_id"])
    if (
        type(listing_map_id) is not int
        or type(row["listing_map_id"]) is not int
        or row["listing_map_id"] != listing_map_id
        or listing_map_id <= 0
        or not isinstance(row["reservation_key"], str)
        or row["reservation_key"] not in member_keys
        or not row["reservation_key"].isascii()
        or not row["reservation_key"].isdigit()
        or int(row["reservation_key"]) <= 0
        or not isinstance(row["scenario"], str)
        or row["scenario"] not in {"01", "06"}
        or row["scenario"] != scenario
        or row["version_status"] != "current"
        or not isinstance(row["change_type"], str)
        or row["change_type"] not in {"personal_update", "phone_update", "cancelled"}
        or type(row["revision"]) is not int
        or row["revision"] < 1
        or type(row["recipient_generation"]) is not int
        or row["recipient_generation"] < 0
    ):
        raise ContactMismatch("current_row_scope")
    _text(row["event_key"], 200, required=True)
    try:
        updated = datetime.fromisoformat(row["source_updated_at"])
        if updated.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError):
        raise ContactMismatch("source_time_invalid") from None
    contact = row["contact"]
    if not isinstance(contact, dict) or set(contact) != CONTACT_KEYS:
        raise ContactMismatch("contact_shape")
    address = contact["address"]
    if not isinstance(address, dict) or set(address) != set(ADDRESS_LIMITS):
        raise ContactMismatch("address_shape")
    clean = {
        "first_name": _text(contact["first_name"], 100, required=True),
        "last_name": _text(contact["last_name"], 100),
        "email": _text(contact["email"], 254),
        "phone": _phone(contact["phone"]),
        "language": contact["language"],
        "address": {k: _text(address[k], size) for k, size in ADDRESS_LIMITS.items()},
    }
    if not isinstance(clean["language"], str) or clean["language"] not in {"ar", "en", "fr"}:
        raise ContactMismatch("source_language_invalid")
    try:
        if clean["email"]:
            validate_email(clean["email"])
    except ValidationError:
        raise ContactMismatch("source_email_invalid") from None
    binding = digest({"chain_id": chain_id, "source": {**row, "contact": clean}})
    if previous:
        if previous.chain_id != chain_id:
            raise ContactMismatch("previous_chain_mismatch")
        if previous.row_id == row_id:
            if previous.binding_sha256 != binding:
                raise ContactMismatch("immutable_source_row_conflict")
            return previous
        if updated <= previous.source_updated_at:
            raise ContactMismatch("stale_or_ambiguous_contact")
        if previous.reservation_key == row["reservation_key"] and (
            row["previous_row_id"] != previous.row_id or row["revision"] <= previous.source_revision
        ):
            raise ContactMismatch("contact_version_chain_mismatch")
    same_booking = previous is not None and previous.reservation_key == row["reservation_key"]
    if type(baseline_generation) is not int or baseline_generation < 0:
        raise ContactMismatch("baseline_generation_invalid")
    old_phone = (
        previous.contact()["phone"]
        if same_booking
        else (_phone(baseline_phone) if baseline_phone else None)
    )
    if row["change_type"] == "cancelled" and (previous is None or clean != previous.contact()):
        raise ContactMismatch("cancelled_contact_must_carry_current")
    changed_phone = clean["phone"] != old_phone
    if previous and clean["phone"] != previous.contact()["phone"] and not changed_phone:
        raise ContactMismatch("contact_requires_current_correction")
    generation = (previous.recipient_generation if same_booking else baseline_generation) + int(
        changed_phone
    )
    if row["recipient_generation"] != generation or (
        changed_phone and row["change_type"] != "phone_update"
    ):
        raise ContactMismatch("recipient_generation_mismatch")
    return ContactProjection(
        chain_id=chain_id,
        row_id=row_id,
        reservation_key=row["reservation_key"],
        source_revision=row["revision"],
        source_updated_at=updated,
        recipient_generation=generation,
        binding_sha256=binding,
        contact_json=json.dumps(clean, sort_keys=True, ensure_ascii=False),
    )


def financial_fingerprint_v2(reservation):
    """Versioned financial identity only, without changing a legacy request.

    Existing payment eligibility, attempts/refunds and live verification remain
    mandatory. A caller must durably tag v2 before using it; never replace a
    legacy fingerprint or infer its version from a failed v1 comparison.
    """
    intent = reservation.booking_intent
    return digest(
        {
            "contract": "site01-payment-financial-2",
            "reservation": str(reservation.pk),
            "intent": str(intent.pk),
            "hostaway": reservation.hostaway_reservation_id,
            "listing": reservation.hostaway_listing_map_id,
            "property": str(reservation.property_id),
            "is_test": reservation.is_test,
            "arrival": str(reservation.check_in),
            "departure": str(reservation.check_out),
            "guests": reservation.guests,
            "total": format(reservation.total_price, ".4f"),
            "currency": reservation.currency,
            "amount": str(intent.payment_amount_sar),
            "fx": intent.exchange_rate_snapshot,
        }
    )


def recipient_message_key(*, booking_key, message_key, recipient_generation):
    """Different recipient generation, same immutable message occurrence.

    A new source row alone never changes this key. This is not a claim/retry
    authorizer and never changes old provider or receipt records.
    """
    if type(recipient_generation) is not int or recipient_generation < 0:
        raise ContactMismatch("recipient_generation_invalid")
    return digest(
        [
            "site01-recipient-message-1",
            _text(booking_key, 100, required=True),
            _text(message_key, 200, required=True),
            recipient_generation,
        ]
    )


def extension_modification_values(extension, contact):
    """Existing verified child-creation notice, with the approved update template.

    Child reference and additional dates are retained. Collection/activation is
    not a prerequisite or inferred result. No ordinary new-booking bundle or
    financial purpose is produced. The result is preparation data, not a send.
    """
    from .services.stay_policy import stay_policy_for
    from .stay_extensions import _checkout, booking_verified

    reservation = extension.reservation
    if (
        contact.chain_id != str(extension.chain_id)
        or not reservation
        or reservation.is_test
        or contact.reservation_key != str(reservation.hostaway_reservation_id)
        or extension.state not in {"created_unpaid", "active"}
        or not booking_verified(extension)
    ):
        raise ContactMismatch("extension_creation_unverified")
    policy = stay_policy_for(extension.chain.property)
    if policy.check_in_hour is None:
        raise ContactMismatch("extension_checkin_time_unavailable")
    checkout, zone = _checkout(extension.chain.property, reservation.check_out)
    person = contact.contact()
    language = person["language"]
    listing = getattr(extension.chain.property, "name_" + language)
    if not listing:
        raise ContactMismatch("extension_listing_name_unavailable")
    child_key = str(reservation.hostaway_reservation_id)
    occurrence = "extension-child-created:" + str(extension.pk)
    keys = (
        "guest_name",
        "booking_reference",
        "listing_name",
        "arrival_date",
        "check_in_time",
        "departure_date",
        "check_out_time",
        "timezone_display",
    )
    values = dict(
        zip(
            keys,
            (
                (person["first_name"] + " " + person["last_name"]).strip(),
                child_key,
                listing,
                reservation.check_in.isoformat(),
                f"{policy.check_in_hour:02}:00",
                reservation.check_out.isoformat(),
                checkout.strftime("%H:%M"),
                zone,
            ),
            strict=True,
        )
    )
    return {
        "contract": "site01-extension-modification-compat-1",
        "reservation_key": child_key,
        "source_message_type": "status_modified",
        "purpose": "booking_change",
        "template_name": "lsa_booking_updated_v2",
        "language": language,
        "ordered_source_keys": list(keys),
        "variables": values,
        "recipient_generation": contact.recipient_generation,
        "message_key": recipient_message_key(
            booking_key=child_key,
            message_key=occurrence,
            recipient_generation=contact.recipient_generation,
        ),
        "source_occurrence": occurrence,
        "payment_authorized": False,
        "send_authorized": False,
    }
