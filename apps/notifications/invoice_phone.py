"""Tolerant invoice-only phone input; national numbers still need a country."""

import re
import unicodedata

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number


def normalize_invoice_phone(value, *, country=None):
    value = unicodedata.normalize("NFKC", value)
    value = "".join(
        str(unicodedata.decimal(char))
        if char.isdecimal()
        else "-"
        if unicodedata.category(char) == "Pd"
        else char
        for char in value
        if unicodedata.category(char) != "Cf"
    )
    if len(value) > 96 or re.search(r"[^0-9+\s().-]", value):
        raise InvalidPhoneNumber
    digits = re.sub(r"[\s().-]", "", value)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+"):
        return normalize_phone_number(digits)
    if not re.fullmatch(r"[0-9]+", digits):
        raise InvalidPhoneNumber
    # Prefer an explicitly selected country for a valid national number. Only
    # try an omitted '+' if national parsing fails; never match phone suffixes.
    if country:
        try:
            return normalize_phone_number(digits, default_region=country)
        except InvalidPhoneNumber:
            pass
    return normalize_phone_number("+" + digits)
