import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template
from django.utils import translation
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()


def _language() -> str:
    return (translation.get_language() or "ar").split("-")[0]


@register.filter
def localized_property_name(property_obj: object) -> str:
    if _language() == "en":
        return (
            getattr(property_obj, "name_en", "")
            or getattr(property_obj, "hostaway_name", "")
            or getattr(property_obj, "name_ar", "")
        )
    return (
        getattr(property_obj, "name_ar", "")
        or getattr(property_obj, "name_en", "")
        or getattr(property_obj, "hostaway_name", "")
    )


@register.filter
def localized_property_description(property_obj: object) -> str:
    if _language() == "en":
        return (
            getattr(property_obj, "description_en", "")
            or getattr(property_obj, "hostaway_description", "")
            or getattr(property_obj, "description_ar", "")
        )
    return (
        getattr(property_obj, "description_ar", "")
        or getattr(property_obj, "description_en", "")
        or getattr(property_obj, "hostaway_description", "")
    )


@register.filter
def localized_city(property_obj: object) -> str:
    if _language() == "en":
        return (
            getattr(property_obj, "city_en", "")
            or getattr(property_obj, "city", "")
            or getattr(property_obj, "city_ar", "")
        )
    return (
        getattr(property_obj, "city_ar", "")
        or getattr(property_obj, "city_en", "")
        or getattr(property_obj, "city", "")
    )


@register.filter
def localized_amenity(amenity: object) -> str:
    if _language() == "en":
        return (
            getattr(amenity, "name_en", "")
            or getattr(amenity, "name", "")
            or getattr(amenity, "name_ar", "")
        )
    return (
        getattr(amenity, "name_ar", "")
        or getattr(amenity, "name_en", "")
        or getattr(amenity, "name", "")
    )


@register.filter
def localized_title(obj: object) -> str:
    suffix = "en" if _language() == "en" else "ar"
    fallback = "ar" if suffix == "en" else "en"
    return getattr(obj, f"title_{suffix}", "") or getattr(obj, f"title_{fallback}", "")


register.filter("localized_page_title", localized_title)


@register.filter
def localized_body(obj: object) -> str:
    suffix = "en" if _language() == "en" else "ar"
    fallback = "ar" if suffix == "en" else "en"
    return getattr(obj, f"body_{suffix}", "") or getattr(obj, f"body_{fallback}", "")


register.filter("localized_page_body", localized_body)


@register.filter
def localized_question(obj: object) -> str:
    suffix = "en" if _language() == "en" else "ar"
    fallback = "ar" if suffix == "en" else "en"
    return getattr(obj, f"question_{suffix}", "") or getattr(obj, f"question_{fallback}", "")


register.filter("localized_faq_question", localized_question)


@register.filter
def localized_answer(obj: object) -> str:
    suffix = "en" if _language() == "en" else "ar"
    fallback = "ar" if suffix == "en" else "en"
    return getattr(obj, f"answer_{suffix}", "") or getattr(obj, f"answer_{fallback}", "")


register.filter("localized_faq_answer", localized_answer)


@register.filter
def rating_out_of_five(value: object) -> str:
    try:
        rating = (Decimal(str(value)) / Decimal("2")).quantize(
            Decimal("0.1"),
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError):
        return ""
    return format(rating, "f")


@register.simple_tag
def json_ld(data: object, nonce: str = "") -> str:
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace(
        "<",
        "\\u003c",
    )
    return format_html(
        '<script type="application/ld+json" nonce="{}">{}</script>',
        nonce,
        mark_safe(serialized),
    )
