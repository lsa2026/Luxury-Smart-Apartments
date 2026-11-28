"""Verified first-created facts on the existing authenticated EXT context GET.

Source20 reuses01/lsa_v32 and owns jobs/generations/claims. No new SourceAuth,
ACK, relay projection, producer, payment gate or schema is introduced here.
"""

from datetime import UTC

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_booking_phone

from .extension_contact_compat import ContactMismatch, extension_modification_source_values
from .models import StayExtensionEvent

CREATED_KIND = "stay_extension.created_unpaid"
CONTRACT = "site01-extension-created-booking-basis-1"


def created_booking_update_basis(extension, event, verified_context):
    if event is None:
        return None
    if event.extension_id != extension.pk:
        raise ValueError("extension_event_scope_mismatch")
    first = (
        StayExtensionEvent.objects.filter(extension=extension, kind=CREATED_KIND)
        .order_by("revision", "created_at", "pk")
        .values_list("pk", flat=True)
        .first()
    )
    current = verified_context
    if (
        event.kind != CREATED_KIND
        or event.pk != first
        or event.revision < 1
        or current.get("chain_revision", 0) < event.revision
        or current.get("chain_revision") != extension.chain.revision
        or current.get("chain_fingerprint") != extension.chain.fingerprint
        or current.get("communication_ready") is not True
        or current.get("booking_verified") is not True
        or extension.state not in {"created_unpaid", "active"}
    ):
        return None
    evidence = current.get("provider_context_v2")
    child = evidence.get("child") if isinstance(evidence, dict) else None
    reservation = extension.reservation
    if (
        not isinstance(child, dict)
        or not reservation
        or reservation.is_test
        or child.get("hostaway_reservation_id") != reservation.hostaway_reservation_id
        or child.get("listing_map_id") != extension.chain.property.hostaway_listing_map_id
        or child.get("status") not in reservation.ACTIVE_STATUSES
    ):
        return None
    contact = child.get("contact")
    if not isinstance(contact, dict) or contact.get("guest_language") not in {"ar", "en", "fr"}:
        return None
    try:
        normalize_booking_phone(contact.get("guest_phone") or "")
        person = {
            "first_name": contact.get("guest_first_name") or "",
            "last_name": contact.get("guest_last_name") or "",
            "language": contact["guest_language"],
        }
        values = extension_modification_source_values(
            extension,
            person,
            chain_id=str(extension.chain_id),
            reservation_key=str(reservation.hostaway_reservation_id),
        )
        if not all(isinstance(value, str) and value.strip() for value in values.values()):
            return None
    except (ContactMismatch, InvalidPhoneNumber, ValueError, KeyError, TypeError):
        return None
    return {
        "contract_version": CONTRACT,
        "extension_id": str(extension.pk),
        "chain_id": str(extension.chain_id),
        "creation_event_id": str(event.pk),
        "creation_event_revision": str(event.revision),
        "creation_occurred_at": event.created_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "root_reservation_key": str(extension.chain.root_hostaway_reservation_id),
        "child_reservation_key": str(reservation.hostaway_reservation_id),
        "listing_map_id": str(extension.chain.property.hostaway_listing_map_id),
        "language": person["language"],
        "values": values,
    }
