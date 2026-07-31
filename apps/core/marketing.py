"""Privacy-safe browser analytics contracts; this module never calls Google."""

import re
from decimal import Decimal
from typing import Any

from django.utils import translation

from apps.core.models import MarketingEventReceipt
from apps.payments.models import PaymentAttempt
from apps.reservations.models import Reservation

EVENT_SCHEMAS: dict[str, frozenset[str]] = {
    "view_item_list": frozenset({"item_list_name", "items"}),
    "select_item": frozenset({"item_list_name", "items"}),
    "view_item": frozenset({"currency", "value", "items"}),
    "begin_checkout": frozenset({"currency", "value", "items", "nights", "guests"}),
    "generate_lead": frozenset({"lead_source"}),
    "check_availability": frozenset({"city", "nights", "guests"}),
    "availability_result": frozenset({"available", "reason_code", "city", "nights"}),
    "quote_created": frozenset({"currency", "value", "nights", "guests", "items"}),
    "quote_expired": frozenset({"reason_code"}),
    "booking_intent_created": frozenset({"currency", "value", "items"}),
    "modification_request_created": frozenset({"request_type"}),
    "cancellation_request_created": frozenset({"request_type"}),
    "contact_form_submitted": frozenset({"lead_source"}),
    "language_changed": frozenset({"language"}),
    "cookie_consent_updated": frozenset({"analytics", "marketing", "version"}),
    "purchase": frozenset({"transaction_id", "value", "currency", "items", "tax", "coupon"}),
    "refund": frozenset({"transaction_id", "value", "currency"}),
}

FORBIDDEN_KEYS = frozenset(
    {
        "email",
        "phone",
        "first_name",
        "last_name",
        "guest_name",
        "address",
        "special_requests",
        "session_key",
        "session_key_hash",
        "hostaway_id",
        "hostaway_listing_id",
        "hostaway_listing_map_id",
        "reservation_id",
        "authorization",
    }
)

ITEM_KEYS = frozenset(
    {
        "item_id",
        "item_name",
        "item_brand",
        "item_category",
        "item_category2",
        "item_category3",
        "index",
        "quantity",
        "currency",
        "price",
    }
)
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Future GTM trigger contract only; no conversion is sent by the server.
GOOGLE_ADS_EVENT_MAP = {
    "contact_success": "generate_lead",
    "booking_confirmed": "purchase",
    "modification_paid": "modification_paid",
}


def _plain_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        return value[:200]
    return None


def sanitize_item(item: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key in ITEM_KEYS:
        value = item.get(key)
        if key == "item_id":
            if not isinstance(value, str) or not SLUG_PATTERN.fullmatch(value):
                continue
        normalized = _plain_value(value)
        if normalized is not None:
            clean[key] = normalized
    return clean


def sanitize_analytics_event(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_name not in EVENT_SCHEMAS:
        raise ValueError("analytics_event_not_allowed")
    if any(key.casefold() in FORBIDDEN_KEYS for key in payload):
        raise ValueError("analytics_payload_contains_forbidden_key")
    clean: dict[str, Any] = {}
    for key in EVENT_SCHEMAS[event_name]:
        if key not in payload:
            continue
        if key == "items":
            items = payload[key]
            if isinstance(items, list):
                clean["items"] = [
                    sanitized
                    for item in items[:20]
                    if isinstance(item, dict) and (sanitized := sanitize_item(item))
                ]
            continue
        normalized = _plain_value(payload[key])
        if normalized is not None:
            clean[key] = normalized
    return clean


def property_analytics_item(
    property_obj: object,
    *,
    index: int = 0,
    price: Decimal | None = None,
    currency: str = "",
) -> dict[str, Any]:
    language = (translation.get_language() or "ar").split("-")[0]
    name_fields = {
        "ar": ("name_ar", "name_en", "hostaway_name"),
        "en": ("name_en", "hostaway_name", "name_ar"),
    }.get(language, ("name_ar", "name_en", "hostaway_name"))
    city_fields = {
        "ar": ("city_ar", "city_en", "city"),
        "en": ("city_en", "city", "city_ar"),
    }.get(language, ("city_ar", "city_en", "city"))
    name = next(
        (value for field_name in name_fields if (value := getattr(property_obj, field_name, ""))),
        "",
    )
    city = next(
        (value for field_name in city_fields if (value := getattr(property_obj, field_name, ""))),
        "",
    )
    item = {
        "item_id": getattr(property_obj, "slug", ""),
        "item_name": name,
        "item_brand": "Luxury Smart Apartments",
        "item_category": "vacation_rental",
        "item_category2": city,
        "item_category3": getattr(property_obj, "room_type", ""),
        "index": index,
        "quantity": 1,
    }
    if price is not None and currency:
        item["currency"] = currency
        item["price"] = price
    return sanitize_item(item)


def prepare_purchase_event(
    *,
    payment_attempt: PaymentAttempt,
    reservation: Reservation,
) -> tuple[dict[str, Any] | None, MarketingEventReceipt | None]:
    """Prepare, but never transmit, a purchase after both authoritative states exist."""
    if payment_attempt.status != PaymentAttempt.Status.SUCCEEDED:
        return None, None
    if (
        reservation.normalized_status != Reservation.Status.CONFIRMED
        or reservation.hostaway_reservation_id is None
    ):
        return None, None
    reference_hash = MarketingEventReceipt.reference_hmac(reservation.public_reference)
    receipt, created = MarketingEventReceipt.objects.get_or_create(
        event_name="purchase",
        object_reference_hash=reference_hash,
        defaults={
            "object_type": "Reservation",
            "status": MarketingEventReceipt.Status.PREPARED,
        },
    )
    if not created and receipt.status == MarketingEventReceipt.Status.EMITTED:
        return None, receipt
    payload = sanitize_analytics_event(
        "purchase",
        {
            "transaction_id": reservation.public_reference,
            "value": reservation.total_price,
            "currency": reservation.currency,
            "items": [property_analytics_item(reservation.property)]
            if reservation.property
            else [],
        },
    )
    return payload, receipt
