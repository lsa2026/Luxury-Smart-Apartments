import json
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template
from django.utils import translation
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_noop

register = template.Library()


def _language() -> str:
    return (translation.get_language() or "ar").split("-")[0]


def _read(obj: object, field_name: str) -> object:
    if isinstance(obj, Mapping):
        return obj.get(field_name, "")
    return getattr(obj, field_name, "")


def _language_order() -> tuple[str, ...]:
    language = _language()
    if language == "fr":
        return ("fr", "en", "ar")
    if language == "en":
        return ("en", "fr", "ar")
    return ("ar", "en", "fr")


def _localized_value(
    obj: object,
    field_name: str,
    *,
    source_fields: tuple[str, ...] = (),
) -> str:
    for language in _language_order():
        value = _read(obj, f"{field_name}_{language}")
        if value:
            return str(value)
        # Hostaway's source content is English. Prefer it at the English
        # fallback position instead of showing a different translated language.
        if language == "en":
            for source_field in source_fields:
                value = _read(obj, source_field)
                if value:
                    return str(value)
    return ""


@register.filter
def localized_field(obj: object, field_name: str) -> str:
    return _localized_value(obj, field_name)


@register.filter
def localized_property_name(property_obj: object) -> str:
    return _localized_value(property_obj, "name", source_fields=("hostaway_name",))


@register.filter
def localized_property_seo_title(property_obj: object) -> str:
    return _localized_value(property_obj, "seo_title") or localized_property_name(property_obj)


@register.filter
def localized_property_description(property_obj: object) -> str:
    return _localized_value(
        property_obj,
        "description",
        source_fields=("hostaway_description",),
    )


@register.filter
def localized_property_meta_description(property_obj: object) -> str:
    return (
        _localized_value(property_obj, "seo_description")
        or _localized_value(property_obj, "short_description")
        or _localized_value(
            property_obj,
            "description",
            source_fields=("hostaway_description",),
        )
    )


@register.filter
def localized_city(property_obj: object) -> str:
    return _localized_value(property_obj, "city", source_fields=("city",))


@register.filter
def localized_amenity(amenity: object) -> str:
    return _localized_value(amenity, "name", source_fields=("name",))


@register.filter
def localized_title(obj: object) -> str:
    return _localized_value(obj, "title")


register.filter("localized_page_title", localized_title)


@register.filter
def localized_body(obj: object) -> str:
    return _localized_value(obj, "body")


register.filter("localized_page_body", localized_body)


@register.filter
def localized_question(obj: object) -> str:
    return _localized_value(obj, "question")


register.filter("localized_faq_question", localized_question)


@register.filter
def localized_answer(obj: object) -> str:
    return _localized_value(obj, "answer")


register.filter("localized_faq_answer", localized_answer)


@register.filter
def localized_image_alt(image: object) -> str:
    value = _localized_value(image, "alt_text") or _localized_value(image, "title")
    if value:
        return value
    property_obj = _read(image, "property")
    if property_obj:
        return localized_property_name(property_obj)
    return str(_read(image, "hostaway_caption") or "")


@register.filter
def localized_image_caption(image: object) -> str:
    return _localized_value(
        image,
        "caption",
        source_fields=("hostaway_caption",),
    )


@register.filter
def localized_review_text(review: object) -> str:
    return _localized_value(
        review,
        "public_review",
        source_fields=("public_review",),
    )


@register.filter
def localized_price_component(title: object) -> str:
    labels = {
        "سعر الإقامة": gettext_noop("Accommodation price"),
        "رسوم التنظيف": gettext_noop("Cleaning fee"),
        "ضريبة القيمة المضافة": gettext_noop("VAT"),
        "الضريبة": gettext_noop("Tax"),
        "ضريبة المدينة": gettext_noop("City tax"),
        "تأمين الأضرار": gettext_noop("Damage deposit"),
        "رسوم ضيف إضافي": gettext_noop("Extra guest fee"),
        "بند سعر": gettext_noop("Price item"),
    }
    source = str(title or "")
    return translation.gettext(labels.get(source, source))


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
