"""Staff search only; a matching phone never establishes guest ownership."""

import re
import unicodedata

import phonenumbers
from django.db.models import CharField, F, Value
from django.db.models.functions import Replace

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number


def phone_variants(value):
    value = unicodedata.normalize("NFKC", value.strip())
    value = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value)
    if len(value) > 80 or not re.fullmatch(r"[+0-9\s().-]+", value):
        return set()
    compact = re.sub(r"[\s().-]", "", value)
    if not 8 <= len(compact.lstrip("+")) <= 17:
        return set()
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    candidates = [compact] if compact.startswith("+") else [compact, "+" + compact]
    normalized = None
    for candidate in candidates:
        try:
            normalized = normalize_phone_number(candidate, default_region="SA")
            break
        except InvalidPhoneNumber:
            continue
    if normalized is None:
        return set()
    parsed = phonenumbers.parse(normalized, None)
    variants = {normalized, normalized[1:], "00" + normalized[1:]}
    # National Saudi input is explicitly supported; other countries use their code.
    if parsed.country_code == 966:
        variants.add("0" + str(parsed.national_number))
        variants.add(str(parsed.national_number))
    return variants


def bookings_for_phone(queryset, value):
    variants = phone_variants(value)
    if not variants:
        return queryset.none()
    expression = F("booking_intent__guest_phone")
    for separator in (" ", "-", "(", ")", ".", "\u00a0"):
        expression = Replace(expression, Value(separator), Value(""), output_field=CharField())
    return queryset.alias(staff_search_phone=expression).filter(staff_search_phone__in=variants)
