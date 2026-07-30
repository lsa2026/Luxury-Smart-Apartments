"""Allowlist-only Unified Webhook parsing and deduplication."""

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils.crypto import salted_hmac


@dataclass(frozen=True, slots=True)
class SanitizedWebhook:
    external_event_id: str | None
    event_type: str
    object_id: str | None
    reservation_id: int | None
    deduplication_key: str
    body_hash: str
    payload: dict[str, Any]


def sanitize_webhook_payload(payload: Any, raw_body: bytes) -> SanitizedWebhook:
    if not isinstance(payload, dict):
        raise ValueError("webhook_payload_not_object")
    containers = _containers(payload)
    event_type = normalize_event_type(
        _first(containers, "eventType", "event_type", "event", "type")
    )
    if not event_type:
        raise ValueError("webhook_event_type_missing")
    external_event_id = _safe_identifier(
        _first(containers, "eventId", "event_id", "webhookEventId")
    )
    object_id = _safe_identifier(_first(containers, "objectId", "object_id"))
    reservation_id = _optional_positive_int(
        _first(
            containers,
            "hostawayReservationId",
            "reservationId",
            "reservation_id",
            "objectId",
        )
    )
    listing_map_id = _optional_positive_int(_first(containers, "listingMapId", "listing_map_id"))
    status = _safe_text(_first(containers, "status", "reservationStatus"), 100)
    arrival_date = _iso_date_text(_first(containers, "arrivalDate", "checkIn"))
    departure_date = _iso_date_text(_first(containers, "departureDate", "checkOut"))
    total_price = _decimal_text(_first(containers, "totalPrice", "total_price"))
    currency = _currency(_first(containers, "currency", "currencyCode"))
    payment_status = _safe_text(
        _first(containers, "paymentStatus", "payment_status"),
        100,
    )
    updated_at = _safe_datetime_text(
        _first(containers, "updatedOn", "latestActivityOn", "updatedAt", "timestamp")
    )
    sanitized = {
        key: value
        for key, value in {
            "event_type": event_type,
            "object_id": object_id,
            "reservation_id": reservation_id,
            "listing_map_id": listing_map_id,
            "status": status,
            "arrival_date": arrival_date,
            "departure_date": departure_date,
            "total_price": total_price,
            "currency": currency,
            "payment_status": payment_status,
            "updated_at": updated_at,
        }.items()
        if value not in (None, "")
    }
    canonical = json.dumps(sanitized, sort_keys=True, separators=(",", ":"))
    deduplication_source = (
        f"external:{external_event_id}" if external_event_id else f"sanitized:{canonical}"
    )
    return SanitizedWebhook(
        external_event_id=external_event_id,
        event_type=event_type,
        object_id=object_id,
        reservation_id=reservation_id,
        deduplication_key=hashlib.sha256(deduplication_source.encode()).hexdigest(),
        body_hash=hashlib.sha256(raw_body).hexdigest(),
        payload=sanitized,
    )


def normalize_event_type(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[\s_-]+", ".", value.strip().casefold())
    aliases = {
        "reservation.created": "reservation.created",
        "reservation.updated": "reservation.updated",
        "new.message.received": "message.received",
    }
    return aliases.get(normalized, normalized[:100])


def webhook_rate_key(remote_address: str) -> str:
    digest = salted_hmac("hostaway-webhook-rate.v1", remote_address).hexdigest()
    return f"hostaway:webhook-rate:{digest}"


def _containers(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    containers = [payload]
    for key in ("data", "result", "object", "reservation"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            containers.append(candidate)
            nested = candidate.get("reservation")
            if isinstance(nested, dict):
                containers.append(nested)
    return tuple(containers)


def _first(containers: tuple[dict[str, Any], ...], *keys: str) -> Any:
    for container in containers:
        for key in keys:
            if key in container and container[key] not in (None, ""):
                return container[key]
    return None


def _safe_identifier(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text[:255] if re.fullmatch(r"[A-Za-z0-9._:-]+", text) else None


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _safe_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value.strip() if char.isprintable())[:limit]


def _iso_date_text(value: Any) -> str:
    text = _safe_text(value, 32)
    return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else ""


def _safe_datetime_text(value: Any) -> str:
    text = _safe_text(value, 40)
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ][0-9:.+-Z]+", text) else ""


def _decimal_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if not parsed.is_finite() or parsed < 0:
        return ""
    return format(parsed, "f")


def _currency(value: Any) -> str:
    text = _safe_text(value, 3).upper()
    return text if len(text) == 3 and text.isascii() and text.isalpha() else ""
