"""Shared parsing for every phone-number value entered into the platform."""

import re
import unicodedata

import phonenumbers


class InvalidPhoneNumber(ValueError):
    """Raised when a value cannot be parsed as a dialable phone number."""


def normalize_phone_number(value: str, *, default_region: str | None = None) -> str:
    """Return a valid number in E.164 format.

    Existing callers remain international-only. Checkout may opt into national
    input using the guest's explicitly selected phone region, never a billing
    country, property location or IP address. Country-specific trunk prefixes
    (including significant Italian zeros) are handled by libphonenumber.
    """
    if default_region is not None and default_region not in phonenumbers.SUPPORTED_REGIONS:
        raise InvalidPhoneNumber
    digits = "".join(str(unicodedata.decimal(char)) if char.isdecimal() else char for char in value)
    if default_region is not None and re.search(r"[^0-9+\s().-]", digits):
        raise InvalidPhoneNumber
    compact = re.sub(r"[^0-9+]", "", digits)
    if default_region is not None and compact.startswith("00"):
        compact = "+" + compact[2:]
    if not re.fullmatch(r"\+?[0-9]+", compact):
        raise InvalidPhoneNumber
    if default_region is None and not compact.startswith("+"):
        raise InvalidPhoneNumber

    try:
        parsed = phonenumbers.parse(compact, default_region)
    except phonenumbers.NumberParseException:
        raise InvalidPhoneNumber from None
    if phonenumbers.is_valid_number(parsed):
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

    raise InvalidPhoneNumber
