"""Shared parsing for every phone-number value entered into the platform."""

import re

import phonenumbers


class InvalidPhoneNumber(ValueError):
    """Raised when a value cannot be parsed as a dialable phone number."""


def normalize_phone_number(value: str, *, default_region: str | None = None) -> str:
    """Return a valid number in E.164 format.

    Explicit international prefixes (``+`` or ``00``) are always interpreted
    independently of a country selection.  A local number is supported only
    when its form supplies an unambiguous ISO country region.
    """
    compact = re.sub(r"[^\d+]", "", value)
    if compact.startswith("00"):
        compact = f"+{compact[2:]}"

    regions: list[str | None] = [None] if compact.startswith("+") else []
    if default_region:
        normalized_region = default_region.upper()
        if normalized_region not in regions:
            regions.append(normalized_region)

    for region in regions:
        try:
            parsed = phonenumbers.parse(compact, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

    raise InvalidPhoneNumber
