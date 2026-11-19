"""Privacy-safe browser analytics contracts; this module never calls Google."""

import math
import re
from decimal import Decimal
from typing import Any

from django.core import signing
from django.utils import translation

from apps.core.models import MarketingEventReceipt
from apps.payments.models import PaymentAttempt
from apps.reservations.models import Reservation

from .assisted_attribution import ENUMS as ATTRIBUTION_ENUMS
from .assisted_attribution import FIELDS as ATTRIBUTION_FIELDS
from .assisted_attribution import KNOWN_CAMPAIGNS, MEDIUMS, SOURCES, purchase_fields

PURCHASE_RECEIPT_TOKEN_SALT = "marketing.purchase-receipt.v1"
PROPERTY_CONTEXT_FIELDS = frozenset({"property_id", "property_name", "page_language"})
PAYMENT_FIELDS = frozenset({"payment_type", "payment_brand", "checkout_kind"})
PAYMENT_JOURNEY_FIELDS = (
    frozenset({"currency", "value", "items", "payment_status", "error_category"})
    | PROPERTY_CONTEXT_FIELDS
    | PAYMENT_FIELDS
)

EVENT_SCHEMAS: dict[str, frozenset[str]] = {
    **{
        name: frozenset(
            {
                "measurement_version",
                "attempt_id",
                "parent_attempt_id",
                "request_kind",
                "entry_page",
                "journey_entry",
                "page_kind",
                "stay_kind",
                "property_id",
                "property_name",
                "page_language",
            }
        )
        | frozenset(extra)
        for name, extra in {
            "availability_view": (),
            "availability_request": ("city", "nights", "guests"),
            "availability_result_view": (
                "city",
                "nights",
                "guests",
                "result_status",
                "display_status",
                "result_count",
                "checked_count",
                "error_count",
                "error_category",
            ),
            "price_view": ("price_context", "currency", "value", "source_currency", "source_value"),
        }.items()
    },
    "view_item_list": frozenset({"item_list_name", "items", "page_language"}),
    "select_item": frozenset({"item_list_name", "items"}) | PROPERTY_CONTEXT_FIELDS,
    "view_item": frozenset({"currency", "value", "items"}) | PROPERTY_CONTEXT_FIELDS,
    "view_all_reviews": (
        frozenset({"language", "review_count", "items"}) | PROPERTY_CONTEXT_FIELDS
    ),
    "begin_checkout": (
        frozenset({"currency", "value", "items", "nights", "guests"}) | PROPERTY_CONTEXT_FIELDS
    ),
    **dict.fromkeys(
        (
            "payment_checkout_requested",
            "secure_payment_view",
            "payment_widget_ready",
            "payment_method_selected",
            "payment_submit_clicked",
            "add_payment_info",
            "payment_widget_error",
            "payment_result",
            "payment_verification_started",
        ),
        PAYMENT_JOURNEY_FIELDS,
    ),
    "generate_lead": frozenset({"lead_source", "page_language"}),
    "check_availability": (
        frozenset({"city", "nights", "guests", "items"}) | PROPERTY_CONTEXT_FIELDS
    ),
    "availability_result": (
        frozenset({"available", "reason_code", "city", "nights", "items"}) | PROPERTY_CONTEXT_FIELDS
    ),
    "quote_created": (
        frozenset({"currency", "value", "nights", "guests", "items"}) | PROPERTY_CONTEXT_FIELDS
    ),
    "quote_expired": frozenset({"reason_code", "items"}) | PROPERTY_CONTEXT_FIELDS,
    "booking_intent_created": (frozenset({"currency", "value", "items"}) | PROPERTY_CONTEXT_FIELDS),
    "modification_request_created": frozenset({"request_type"}),
    "cancellation_request_created": frozenset({"request_type"}),
    "contact_form_submitted": frozenset({"lead_source", "page_language"}),
    "whatsapp_click": (
        frozenset({"lead_source", "language", "contact_placement"}) | PROPERTY_CONTEXT_FIELDS
    ),
    "phone_click": (
        frozenset({"lead_source", "language", "contact_placement"}) | PROPERTY_CONTEXT_FIELDS
    ),
    "language_changed": frozenset({"language"}),
    "cookie_consent_updated": frozenset({"analytics", "marketing", "version"}),
    "purchase": (
        frozenset({"transaction_id", "value", "currency", "items", "tax", "coupon"})
        | PROPERTY_CONTEXT_FIELDS
        | PAYMENT_FIELDS
        | frozenset(ATTRIBUTION_FIELDS)
    ),
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
    if event_name == "purchase" and any(key in payload for key in ATTRIBUTION_FIELDS):
        if payload.get("attribution_version") != "aa1":
            raise ValueError("invalid_attribution_version")
        for key in ATTRIBUTION_FIELDS:
            value = payload.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError("invalid_attribution_type")
            if key in ATTRIBUTION_ENUMS and value not in ATTRIBUTION_ENUMS[key]:
                raise ValueError("invalid_attribution_enum")
            if key in {"first_source", "last_source"} and value not in SOURCES:
                raise ValueError("invalid_attribution_source")
            if key in {"first_medium", "last_medium"} and value not in MEDIUMS:
                raise ValueError("invalid_attribution_medium")
            if key in {"first_campaign_id", "last_campaign_id"} and value not in KNOWN_CAMPAIGNS:
                raise ValueError("invalid_attribution_campaign")
            if key == "origin_property_id" and not SLUG_PATTERN.fullmatch(value):
                raise ValueError("invalid_attribution_property")
    if event_name in {
        "availability_view",
        "availability_request",
        "availability_result_view",
        "price_view",
    }:
        from apps.reservations.prepurchase import DISPLAY, PAGES

        enums = {
            "measurement_version": {"pp1"},
            "request_kind": {"portfolio", "property_quote", "browse"},
            "entry_page": PAGES,
            "page_kind": PAGES,
            "journey_entry": PAGES,
            "stay_kind": {"overnight"},
            "city": {"riyadh", "marrakech", "all", "other"},
            "result_status": set(DISPLAY),
            "display_status": set(DISPLAY.values()),
            "error_category": {
                "none",
                "validation",
                "rate_limit",
                "provider",
                "pricing",
                "internal",
            },
            "price_context": {
                "indicative_nightly",
                "calendar_nightly",
                "search_total",
                "quote_total",
            },
        }
        if payload.get("measurement_version") != "pp1":
            raise ValueError("invalid_prepurchase_version")
        for key, raw in payload.items():
            if raw is None:
                continue
            value = _plain_value(raw)
            if key not in EVENT_SCHEMAS[event_name] or isinstance(value, bool) or value is None:
                raise ValueError("invalid_prepurchase_field")
            if key in {"property_id", "property_name", "page_language"} and not isinstance(
                value, str
            ):
                raise ValueError("invalid_prepurchase_text")
            if key in enums and (not isinstance(value, str) or value not in enums[key]):
                raise ValueError("invalid_prepurchase_enum")
            if key in {"attempt_id", "parent_attempt_id"} and not re.fullmatch(
                r"[a-f0-9]{32}", str(value)
            ):
                raise ValueError("invalid_prepurchase_correlation")
            if key in {"currency", "source_currency"} and not re.fullmatch(r"[A-Z]{3}", str(value)):
                raise ValueError("invalid_prepurchase_currency")
            if key in {"value", "source_value"} and (
                not isinstance(value, int | float) or not math.isfinite(value) or value <= 0
            ):
                raise ValueError("invalid_prepurchase_amount")
            if key in {"nights", "guests", "result_count", "checked_count", "error_count"}:
                low, high = (
                    (1, 100) if key == "guests" else (1, 3660) if key == "nights" else (0, 10000)
                )
                if not isinstance(value, int) or not low <= value <= high:
                    raise ValueError("invalid_prepurchase_count")
        if event_name == "price_view" and (
            payload.get("value") is None or not payload.get("currency")
        ):
            raise ValueError("missing_prepurchase_price")
        if event_name == "availability_result_view" and (
            not payload.get("result_status") or not payload.get("display_status")
        ):
            raise ValueError("missing_prepurchase_outcome")
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
        if key == "property_id":
            value = payload[key]
            if not isinstance(value, str) or not SLUG_PATTERN.fullmatch(value):
                continue
        if key == "page_language":
            value = payload[key]
            if not isinstance(value, str) or not re.fullmatch(r"[a-z]{2,3}", value):
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
        "ar": ("name_ar", "name_en", "hostaway_name", "name_fr"),
        "en": ("name_en", "hostaway_name", "name_fr", "name_ar"),
        "fr": ("name_fr", "name_en", "hostaway_name", "name_ar"),
    }.get(language, ("name_ar", "name_en", "hostaway_name", "name_fr"))
    city_fields = {
        "ar": ("city_ar", "city_en", "city", "city_fr"),
        "en": ("city_en", "city", "city_fr", "city_ar"),
        "fr": ("city_fr", "city_en", "city", "city_ar"),
    }.get(language, ("city_ar", "city_en", "city", "city_fr"))
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
    request=None,
) -> tuple[dict[str, Any] | None, MarketingEventReceipt | None]:
    """Prepare, but never transmit, a purchase after both authoritative states exist."""
    if (
        payment_attempt.status != PaymentAttempt.Status.SUCCEEDED
        or getattr(payment_attempt, "provider", "hyperpay") != "hyperpay"
        or getattr(reservation, "is_test", False)
        or (request is not None and getattr(getattr(request, "user", None), "is_staff", False))
    ):
        return None, None
    if (
        reservation.normalized_status != Reservation.Status.CONFIRMED
        or reservation.hostaway_reservation_id is None
    ):
        return None, None
    has_payment_request = bool(getattr(payment_attempt, "payment_request_id", None))
    if has_payment_request:
        from apps.payments.purchase_receipts import eligible, receipt_for

        # Both result routes use the existing capture-scoped ledger; no second sender.
        if not eligible(payment_attempt):
            return None, None
        receipt = receipt_for(payment_attempt)
        reference_hash = receipt.object_reference_hash
    else:
        reference_hash = MarketingEventReceipt.reference_hmac(reservation.public_reference)
        receipt, _ = MarketingEventReceipt.objects.get_or_create(
            event_name="purchase",
            object_reference_hash=reference_hash,
            defaults={
                "object_type": "Reservation",
                "status": MarketingEventReceipt.Status.PREPARED,
            },
        )
    if receipt.status != MarketingEventReceipt.Status.PREPARED:
        return None, receipt
    property_obj = (
        reservation.property
        if reservation.property_id
        else reservation.booking_intent.property
        if reservation.booking_intent_id
        else None
    )
    financial_value = payment_attempt.amount if has_payment_request else reservation.total_price
    financial_currency = payment_attempt.currency if has_payment_request else reservation.currency
    purchased_item = (
        property_analytics_item(
            property_obj,
            price=financial_value,
            currency=financial_currency,
        )
        if property_obj is not None
        else None
    )
    payload = sanitize_analytics_event(
        "purchase",
        {
            # Expose only an opaque, non-reversible identifier to the browser.
            "transaction_id": reference_hash,
            "value": financial_value,
            "currency": financial_currency,
            **({"checkout_kind": "payment_link"} if has_payment_request else {}),
            "page_language": (translation.get_language() or "ar").split("-")[0],
            **payment_analytics_fields(payment_attempt),
            **purchase_fields(request, payment_attempt, reservation),
            **(
                {
                    "property_id": purchased_item["item_id"],
                    "property_name": purchased_item.get("item_name", ""),
                    "items": [purchased_item],
                }
                if purchased_item and purchased_item.get("item_id")
                else {}
            ),
        },
    )
    return payload, receipt


def payment_analytics_fields(attempt: PaymentAttempt) -> dict[str, str]:
    """Keep the wallet separate from the network; missing evidence is unknown."""
    brands = {"MADA": "mada", "VISA": "visa", "MASTER": "mastercard", "APPLEPAY": "apple_pay"}
    brand = brands.get(getattr(attempt, "payment_brand", ""), "unknown")
    return {
        "payment_brand": brand,
        "payment_type": "apple_pay"
        if getattr(attempt, "payment_wallet", "") == "apple_pay" or brand == "apple_pay"
        else "unknown",
    }


def purchase_receipt_token(receipt: MarketingEventReceipt) -> str:
    """Issue a signed, opaque acknowledgement token for one prepared purchase."""
    if receipt.event_name != "purchase" or receipt.status != MarketingEventReceipt.Status.PREPARED:
        raise ValueError("Only prepared purchase receipts can be acknowledged.")
    return signing.dumps(
        {"receipt_id": str(receipt.pk), "event_name": receipt.event_name},
        salt=PURCHASE_RECEIPT_TOKEN_SALT,
        compress=True,
    )
