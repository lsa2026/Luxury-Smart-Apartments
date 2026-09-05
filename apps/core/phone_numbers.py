"""Shared parsing for every phone-number value entered into the platform."""

import re

import phonenumbers


class InvalidPhoneNumber(ValueError):
    """Raised when a value cannot be parsed as a dialable phone number."""


def normalize_phone_number(value: str) -> str:
    """Return a valid number in E.164 format.

    Phone numbers entered in the platform must be in international form.  The
    leading ``+`` and country calling code make the number unambiguous and
    prevent a local number from being silently interpreted using another field.
    """
    compact = re.sub(r"[^\d+]", "", value)
    if not compact.startswith("+"):
        raise InvalidPhoneNumber

    try:
        parsed = phonenumbers.parse(compact, None)
    except phonenumbers.NumberParseException:
        raise InvalidPhoneNumber from None
    if phonenumbers.is_valid_number(parsed):
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

    raise InvalidPhoneNumber
