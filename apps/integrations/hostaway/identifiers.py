"""Lossless identities at the Hostaway payment boundary; never parse IDs as floats."""

import re

from .exceptions import HostawayResponseError

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", re.ASCII)


def payment_identifier(value: object) -> str:
    """Accept legacy JSON integers and opaque text, retaining every character."""
    if type(value) is int and value > 0:
        value = str(value)
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or value == "0":
        raise HostawayResponseError("Invalid Hostaway payment identity.")
    return value


def normalize_guest_charge(record: object, *, reservation_id: object) -> dict:
    if not isinstance(record, dict):
        raise HostawayResponseError("Invalid guest charge record.")
    identifier = payment_identifier(record.get("id"))
    reservation = payment_identifier(record.get("reservationId"))
    if reservation != payment_identifier(reservation_id):
        raise HostawayResponseError("Guest charges reservation mismatch.")
    return {**record, "id": identifier, "reservationId": reservation}
