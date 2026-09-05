import json
from collections.abc import Mapping
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

from django import template
from django.utils import formats, timezone, translation
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_noop

register = template.Library()

_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "ISK",
        "JPY",
        "KMF",
        "KRW",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)
_THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})
_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


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
    """Render a known Hostaway amenity in the active guest language.

    A sync can legitimately create a new amenity before its translated database
    fields have been seeded.  The curated map is therefore the safe immediate
    fallback; it prevents an otherwise Arabic or French property page from
    flashing an English provider label during that gap.
    """
    language = _language()
    if language in {"ar", "fr"} and not _read(amenity, f"name_{language}"):
        from apps.properties.amenity_translations import copy_for

        entry = copy_for(str(_read(amenity, "name") or _read(amenity, "name_en")))
        if entry is not None:
            return entry.name_ar if language == "ar" else entry.name_fr
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
def structured_text(value: object) -> str:
    """Render a small, safe subset of Markdown used by editable content pages."""
    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{'<br>'.join(escape(line) for line in paragraph)}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            items = "".join(f"<li>{escape(item)}</li>" for item in list_items)
            output.append(f"<ul>{items}</ul>")
            list_items.clear()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
        elif line.startswith("### "):
            flush_paragraph()
            flush_list()
            output.append(f"<h3>{escape(line[4:])}</h3>")
        elif line.startswith("## "):
            flush_paragraph()
            flush_list()
            output.append(f"<h2>{escape(line[3:])}</h2>")
        elif line.startswith("- "):
            flush_paragraph()
            list_items.append(line[2:].strip())
        else:
            flush_list()
            paragraph.append(line)

    flush_paragraph()
    flush_list()
    return mark_safe("".join(output))


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
def review_body(review: object) -> object:
    """Split a review into headline, positives and negatives for display.

    Hostaway merges the channel's structured answers into one string with
    English markers. Reading them here keeps the stored row untouched while the
    page shows Arabic headings and drops a section that says nothing.
    """
    from apps.reviews.presentation import parse_review_body

    return parse_review_body(localized_review_text(review))


@register.filter
def localized_price_component(title: object) -> str:
    labels = {
        "سعر الإقامة": gettext_noop("Accommodation price"),
        "Accommodation price": gettext_noop("Accommodation price"),
        "Base rate": gettext_noop("Base rate"),
        "رسوم التنظيف": gettext_noop("Cleaning fee"),
        "Cleaning fee": gettext_noop("Cleaning fee"),
        "Service fee": gettext_noop("Service fee"),
        "ضريبة القيمة المضافة": gettext_noop("VAT"),
        "VAT": gettext_noop("VAT"),
        "الضريبة": gettext_noop("Tax"),
        "Tax": gettext_noop("Tax"),
        "ضريبة المدينة": gettext_noop("City tax"),
        "City tax": gettext_noop("City tax"),
        "تأمين الأضرار": gettext_noop("Damage deposit"),
        "Damage deposit": gettext_noop("Damage deposit"),
        "رسوم ضيف إضافي": gettext_noop("Extra guest fee"),
        "Extra guest fee": gettext_noop("Extra guest fee"),
        "Pet fee": gettext_noop("Pet fee"),
        "Resort fee": gettext_noop("Resort fee"),
        "Weekly discount": gettext_noop("Weekly discount"),
        "Monthly discount": gettext_noop("Monthly discount"),
        "Coupon discount": gettext_noop("Coupon discount"),
        "Discount": gettext_noop("Discount"),
        "بند سعر": gettext_noop("Price item"),
        "Price item": gettext_noop("Price item"),
    }
    source = str(title or "")
    return translation.gettext(labels.get(source, source))


@register.filter
def price_component_amount(component: object) -> object:
    """Prefer the authoritative component total, with legacy quote compatibility."""
    if isinstance(component, Mapping):
        if "total" in component:
            return component["total"]
        return component.get("value", "")
    total = getattr(component, "total", None)
    if total is not None:
        return total
    return getattr(component, "value", "")


@register.filter
def localized_room_type(value: object) -> str:
    labels = {
        "entire_home": gettext_noop("Entire home"),
        "private_room": gettext_noop("Private room"),
        "shared_room": gettext_noop("Shared room"),
        "hotel_room": gettext_noop("Hotel room"),
    }
    source = str(value or "")
    return translation.gettext(labels.get(source, source.replace("_", " ").title()))


RESERVATION_STATUS_TONES = {
    "confirmed": "positive",
    "modified": "positive",
    "cancelled": "closed",
    "declined": "closed",
    "expired": "closed",
    "create_failed": "closed",
}

MODIFICATION_STATUS_TONES = {
    "completed": "positive",
    "rejected": "closed",
    "expired": "closed",
    "unavailable": "closed",
    "failed": "closed",
}


@register.filter
def localized_cancellation_policy(value: object) -> str:
    """Hostaway's policy identifier in words the guest can act on."""
    labels = {
        "flexible": gettext_noop("Flexible cancellation"),
        "moderate": gettext_noop("Moderate cancellation"),
        "strict": gettext_noop("Strict cancellation"),
        "firm": gettext_noop("Firm cancellation"),
        "super_strict": gettext_noop("Very strict cancellation"),
        "non_refundable": gettext_noop("Non-refundable"),
    }
    source = str(value or "").strip().casefold()
    if not source:
        return ""
    return translation.gettext(labels.get(source, gettext_noop("Host cancellation policy")))


@register.filter
def reservation_status_tone(value: object) -> str:
    """Colour family for a reservation pill: positive, closed, or in progress."""
    return RESERVATION_STATUS_TONES.get(str(value or ""), "progress")


@register.filter
def modification_status_tone(value: object) -> str:
    """Colour family for a change-request pill."""
    return MODIFICATION_STATUS_TONES.get(str(value or ""), "progress")


@register.filter
def localized_reservation_status(value: object) -> str:
    labels = {
        "awaiting_payment": gettext_noop("Awaiting payment"),
        "ready_for_hostaway": gettext_noop("Preparing confirmation"),
        "create_pending": gettext_noop("Preparing confirmation"),
        "creating": gettext_noop("Confirming booking"),
        "confirmed": gettext_noop("Confirmed"),
        "create_failed": gettext_noop("Needs assistance"),
        "create_unknown": gettext_noop("Under review"),
        "sync_pending": gettext_noop("Updating"),
        "modified": gettext_noop("Updated"),
        "cancelled": gettext_noop("Cancelled"),
        # Statuses a channel can report for a stay that never completed. Without
        # their own wording a declined guest would read "Under review" forever.
        "pending": gettext_noop("Awaiting confirmation"),
        "inquiry": gettext_noop("Enquiry"),
        "declined": gettext_noop("Not accepted"),
        "expired": gettext_noop("Offer expired"),
        "unknown": gettext_noop("Under review"),
    }
    source = str(value or "")
    return translation.gettext(labels.get(source, gettext_noop("Under review")))


@register.filter
def localized_payment_status(value: object) -> str:
    labels = {
        "": gettext_noop("Not paid"),
        "created": gettext_noop("Payment pending"),
        "pending": gettext_noop("Payment pending"),
        "paid": gettext_noop("Payment verified"),
        "succeeded": gettext_noop("Payment verified"),
        "sandbox_paid": gettext_noop("Test payment completed"),
        "failed": gettext_noop("Payment was not successful"),
        "cancelled": gettext_noop("Payment was cancelled"),
        "refunded": gettext_noop("Refunded"),
        "partially_refunded": gettext_noop("Partially refunded"),
        "review": gettext_noop("Payment needs review"),
        "unknown": gettext_noop("Payment needs review"),
    }
    source = str(value or "")
    return translation.gettext(labels.get(source, gettext_noop("Under review")))


@register.filter
def localized_modification_status(value: object) -> str:
    labels = {
        "draft": gettext_noop("Draft"),
        "pending_revalidation": gettext_noop("Checking availability"),
        "awaiting_customer_approval": gettext_noop("Awaiting your approval"),
        "awaiting_payment": gettext_noop("Awaiting payment"),
        "pending_admin_approval": gettext_noop("Under review"),
        "ready_for_hostaway": gettext_noop("Ready to update"),
        "processing": gettext_noop("Updating"),
        "completed": gettext_noop("Completed"),
        "rejected": gettext_noop("Declined"),
        "expired": gettext_noop("Expired"),
        "price_changed": gettext_noop("Price changed"),
        "unavailable": gettext_noop("Unavailable"),
        "failed": gettext_noop("Needs assistance"),
        "unknown": gettext_noop("Under review"),
    }
    source = str(value or "")
    return translation.gettext(labels.get(source, gettext_noop("Under review")))


@register.filter
def localized_modification_type(value: object) -> str:
    labels = {
        "extend_stay": gettext_noop("Extend stay"),
        "change_dates": gettext_noop("Change dates"),
        "change_guests": gettext_noop("Change guests"),
        "cancel_reservation": gettext_noop("Cancellation request"),
    }
    source = str(value or "")
    return translation.gettext(labels.get(source, source.replace("_", " ").title()))


@register.filter
def money_amount(value: object) -> str:
    try:
        amount = Decimal(str(value)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError):
        return str(value or "")
    return format(amount, ",.2f")


def _as_local(value: object) -> object:
    """Move an aware datetime into the site's timezone before it is formatted.

    Django converts aware datetimes when the template prints them itself, but a
    filter is handed the raw value and ``formats.date_format`` does no
    conversion at all. Without this every timestamp rendered through these
    filters is printed in UTC, three hours behind Riyadh, which also drags the
    date back a day for anything after 21:00 local.

    ``date`` objects carry no time and must be left alone; ``localtime`` also
    refuses naive datetimes, so both fall through unchanged.
    """
    if not isinstance(value, datetime):
        return value
    if timezone.is_naive(value):
        return value
    return timezone.localtime(value)


def _localized_temporal(value: object, format_string: str) -> str:
    """Render a date/time in the active locale, matching the numerals used for money."""
    if value in (None, ""):
        return ""
    try:
        rendered = formats.date_format(_as_local(value), format_string)
    except (AttributeError, TypeError, ValueError):
        return ""
    if _language() == "ar":
        return rendered.translate(_ARABIC_DIGITS)
    return rendered


@register.filter
def localized_date(value: object) -> str:
    """Long-form date such as ``25 November 2026`` or ``٢٥ نوفمبر ٢٠٢٦``."""
    return _localized_temporal(value, "j F Y")


@register.filter
def localized_datetime(value: object) -> str:
    """Long-form date and 24-hour time in the active locale."""
    return _localized_temporal(value, "j F Y H:i")


@register.filter
def localized_month_year(value: object) -> str:
    """Month and year only, used for review timestamps."""
    return _localized_temporal(value, "F Y")


class LocalizedCount(int):
    """An int that renders with the active language's numerals.

    ``{% blocktrans count %}`` needs a real number to pick the plural form, so a
    plain localized string cannot be passed. Subclassing ``int`` keeps plural
    selection intact while changing only how the value is printed, which leaves
    every existing msgid — and its Arabic plural forms — untouched.
    """

    __slots__ = ()

    def __str__(self) -> str:
        if _language() == "ar":
            return super().__str__().translate(_ARABIC_DIGITS)
        return super().__str__()

    def __format__(self, format_spec: str) -> str:
        return self.__str__() if format_spec == "" else super().__format__(format_spec)


@register.filter
def localized_count(value: object) -> object:
    """Wrap a count so ``blocktrans`` prints it in the active language's numerals."""
    try:
        return LocalizedCount(value)
    except (TypeError, ValueError):
        return value


@register.filter
def localized_decimal(value: object) -> str:
    """A one-place decimal such as 4.1, in the active language's numerals."""
    if value in (None, ""):
        return ""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return ""
    text = f"{amount}"
    if _language() == "ar":
        return text.translate(_ARABIC_DIGITS)
    return text


@register.filter
def localized_number(value: object) -> str:
    """Render a plain count with the same numerals used for money and dates."""
    if value in (None, ""):
        return ""
    text = str(value)
    if _language() == "ar":
        return text.translate(_ARABIC_DIGITS)
    return text


@register.filter
def localized_hour(value: object) -> str:
    """Render an hour-of-day as ``HH:00``; 24 is the midnight that ends the day."""
    if not isinstance(value, int) or isinstance(value, bool):
        return ""
    if not 0 <= value <= 24:
        return ""
    text = f"{value % 24:02d}:00"
    if _language() == "ar":
        return text.translate(_ARABIC_DIGITS)
    return text


@register.filter
def localized_percentage(value: object) -> str:
    """Render a refund share without trailing zeros: 100, 50, 12.5."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    normalized = amount.quantize(Decimal("0.01")).normalize()
    if normalized == normalized.to_integral_value():
        text = f"{int(normalized)}%"
    else:
        text = f"{normalized}%"
    if _language() == "ar":
        return text.translate(_ARABIC_DIGITS)
    return text


# An Arabic page showing "SAR" reads as untranslated, so the currencies this
# site actually prices in carry their Arabic symbol. A currency absent here
# keeps its ISO code, which is correct everywhere and wrong nowhere.
_ARABIC_CURRENCY_SYMBOLS: Final = {
    "SAR": "ر.س",
    "MAD": "د.م",
    "AED": "د.إ",
    "KWD": "د.ك",
    "BHD": "د.ب",
    "QAR": "ر.ق",
    "OMR": "ر.ع",
    "EGP": "ج.م",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
}


def _currency_label(currency_code: str, language: str) -> str:
    """The currency as this language writes it."""
    if language == "ar":
        return _ARABIC_CURRENCY_SYMBOLS.get(currency_code, currency_code)
    return currency_code


def _currency_precision(currency: str) -> int:
    if currency in _ZERO_DECIMAL_CURRENCIES:
        return 0
    if currency in _THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def _localized_money_number(amount: Decimal, language: str, precision: int) -> str:
    number = format(amount, f",.{precision}f")
    whole, separator, fraction = number.partition(".")

    if language == "fr":
        whole = whole.replace(",", "\u202f")
        return whole + (f",{fraction}" if separator else "")

    if language == "ar":
        whole = whole.replace(",", "٬").translate(_ARABIC_DIGITS)
        fraction = fraction.translate(_ARABIC_DIGITS)
        return whole + (f"٫{fraction}" if separator else "")

    return number


def _localized_money_parts(
    value: object,
    currency: object,
    *,
    language: str | None = None,
) -> tuple[str, str, str, str] | None:
    currency_code = str(currency or "").strip().upper()
    if len(currency_code) != 3 or not currency_code.isascii() or not currency_code.isalpha():
        currency_code = ""

    precision = _currency_precision(currency_code)
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
        amount = amount.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None

    active_language = (language or _language()).split("-")[0].lower()
    number = _localized_money_number(amount, active_language, precision)
    return active_language, currency_code, number, _currency_label(currency_code, active_language)


def localized_money_text(
    value: object,
    currency: object,
    *,
    language: str | None = None,
) -> str:
    """Return a copyable, locale-aware monetary value for text-only surfaces.

    The non-breaking space keeps an amount and its currency together in emails,
    exports, and narrow cards.  It also makes the decimal separator unambiguous:
    Arabic uses ``٫`` (not a comma), French uses ``,`` and English uses ``.``.
    """
    parts = _localized_money_parts(value, currency, language=language)
    if parts is None:
        return "—"

    active_language, currency_code, number, currency_label = parts
    if not currency_code:
        return number
    separator = "\u00a0"
    if active_language == "en":
        return f"{currency_label}{separator}{number}"
    return f"{number}{separator}{currency_label}"


@register.filter
def localized_money(value: object, currency: object) -> str:
    """Render a price with locale-aware separators and ISO-4217 precision."""
    parts = _localized_money_parts(value, currency)
    if parts is None:
        return format_html(
            '<bdi class="money money--unavailable" dir="ltr">{}</bdi>',
            "—",
        )

    language, currency_code, number, currency_label = parts
    if not currency_code:
        return format_html(
            '<bdi class="money money--{}" dir="ltr"><span class="money__amount">{}</span></bdi>',
            language,
            number,
        )

    if language == "en":
        return format_html(
            '<bdi class="money money--en" dir="ltr">'
            '<span class="money__currency">{}</span>'
            '<span class="money__separator">{}</span>'
            '<span class="money__amount">{}</span>'
            "</bdi>",
            currency_label,
            "\u00a0",
            number,
        )

    return format_html(
        '<bdi class="money money--{}" dir="ltr">'
        '<span class="money__amount">{}</span>'
        '<span class="money__separator">{}</span>'
        '<span class="money__currency" aria-label="{}">{}</span>'
        "</bdi>",
        language,
        number,
        "\u00a0",
        currency_code,
        currency_label,
    )


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
