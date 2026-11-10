"""Shared parsing for every phone-number value entered into the platform."""

import re
import unicodedata

import phonenumbers


def normalize_booking_phone(value: str, *, default_region: str = "SA") -> str:
    """Booking input: explicit international prefixes win; otherwise selected/SA region."""
    region = default_region or "SA"
    try:
        return normalize_phone_number(value, default_region=region)
    except InvalidPhoneNumber:
        digits = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value).strip()
        if digits and digits[0].isdigit():
            return normalize_phone_number("+" + digits, default_region=region)
        raise


def normalize_comparison_phone(value: str) -> str:
    """Compare complete E.164 numbers, never suffixes or country-stripped digits."""
    value = unicodedata.normalize("NFKC", value.strip())
    value = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value)
    if len(value) > 80 or not re.fullmatch(r"[+0-9\s().-]+", value):
        raise InvalidPhoneNumber
    compact = re.sub(r"[\s().-]", "", value)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if compact.startswith("+"):
        return normalize_phone_number(compact)
    if not compact.startswith("0"):
        try:
            return normalize_phone_number("+" + compact)
        except InvalidPhoneNumber:
            pass
    # The existing Saudi national convention is supported; another country's
    # ambiguous national number never inherits a guessed foreign calling code.
    return normalize_phone_number(compact, default_region="SA")


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
