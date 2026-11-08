"""Morning-only support for pasted country-code numbers without a plus sign."""

import unicodedata

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number


def normalize_morning_phone(value, country="SA"):
    try:
        return normalize_phone_number(value, default_region=country)
    except InvalidPhoneNumber:
        # A full international number is accepted only if the shared parser
        # validates it. Never assign US merely because a string starts with 1.
        digits = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value).strip()
        if digits and digits[0].isdigit():
            return normalize_phone_number("+" + digits, default_region=country)
        raise
