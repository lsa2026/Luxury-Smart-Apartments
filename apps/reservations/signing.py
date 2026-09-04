"""Server-side HMAC helpers for quote integrity and opaque URL references."""

import json
from decimal import Decimal
from typing import Any

from django.core import signing
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import BookingQuote

QUOTE_FINGERPRINT_SALT = "luxury-smart-apartments.booking-quote.v1"
QUOTE_REFERENCE_SALT = "luxury-smart-apartments.booking-quote-reference.v1"


def quote_fingerprint_values(
    *,
    quote_id: object,
    property_id: object,
    hostaway_listing_id: int,
    check_in: object,
    check_out: object,
    guests: int,
    currency: str,
    total_price: Decimal,
    price_version: int,
    payment_amount_sar: Decimal | None = None,
    selected_display_currency: str = "",
    exchange_rate_snapshot: dict[str, Any] | None = None,
) -> str:
    payload = {
        "quote_id": str(quote_id),
        "property_id": str(property_id),
        "hostaway_listing_id": hostaway_listing_id,
        "check_in": str(check_in),
        "check_out": str(check_out),
        "guests": guests,
        "currency": currency,
        "total_price": format(total_price.normalize(), "f"),
        "price_version": price_version,
    }
    # Legacy, short-lived quotes did not contain FX fields. Keeping their exact
    # v1 payload verifies them during a rolling deployment; every newly created
    # quote includes and signs the complete payment snapshot below.
    if payment_amount_sar is not None or exchange_rate_snapshot:
        payload.update(
            {
                "payment_amount_sar": (
                    format(payment_amount_sar.normalize(), "f")
                    if payment_amount_sar is not None
                    else None
                ),
                "selected_display_currency": selected_display_currency,
                "exchange_rate_snapshot": exchange_rate_snapshot or {},
            }
        )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return salted_hmac(QUOTE_FINGERPRINT_SALT, canonical).hexdigest()


def quote_fingerprint(quote: BookingQuote) -> str:
    return quote_fingerprint_values(
        quote_id=quote.pk,
        property_id=quote.property_id,
        hostaway_listing_id=quote.hostaway_listing_id,
        check_in=quote.check_in,
        check_out=quote.check_out,
        guests=quote.guests,
        currency=quote.currency,
        total_price=quote.total_price,
        price_version=quote.price_version,
        payment_amount_sar=quote.payment_amount_sar,
        selected_display_currency=quote.selected_display_currency,
        exchange_rate_snapshot=quote.exchange_rate_snapshot,
    )


def verify_quote_fingerprint(quote: BookingQuote) -> bool:
    return constant_time_compare(quote.signature, quote_fingerprint(quote))


def quote_reference(quote: BookingQuote) -> str:
    return signing.dumps(str(quote.pk), salt=QUOTE_REFERENCE_SALT, compress=True)


def quote_id_from_reference(reference: str) -> str:
    value: Any = signing.loads(reference, salt=QUOTE_REFERENCE_SALT)
    if not isinstance(value, str):
        raise signing.BadSignature("Invalid quote reference.")
    return value
