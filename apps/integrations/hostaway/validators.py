"""Validation and normalization for untrusted Hostaway JSON."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .exceptions import HostawayResponseError


@dataclass(frozen=True, slots=True)
class HostawayReview:
    hostaway_review_id: int
    hostaway_listing_map_id: int
    hostaway_reservation_id: str
    external_review_id: str
    channel_id: str
    review_type: str
    status: str
    guest_name: str
    rating: Decimal | None
    public_review: str
    reviewee_response: str
    arrival_date: date | None
    departure_date: date | None
    source_updated_at: datetime | None


def validate_reviews_page(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Validate the top-level reviews response without trusting nested records."""
    if not isinstance(payload, dict):
        raise HostawayResponseError("Hostaway reviews response must be a JSON object.")
    if payload.get("status") not in (None, "success"):
        raise HostawayResponseError("Hostaway reviews response reports a failure.")

    result = payload.get("result")
    if not isinstance(result, list):
        raise HostawayResponseError("Hostaway reviews response is missing a result list.")
    if not all(isinstance(item, dict) for item in result):
        raise HostawayResponseError("Hostaway reviews result contains a non-object item.")

    raw_count = payload.get("count")
    if raw_count is not None and (
        not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count < 0
    ):
        raise HostawayResponseError("Hostaway reviews count must be a non-negative integer.")

    raw_total = payload.get("total")
    if raw_total is None:
        return result, None
    if not isinstance(raw_total, int) or isinstance(raw_total, bool) or raw_total < 0:
        raise HostawayResponseError("Hostaway reviews total must be a non-negative integer.")
    return result, raw_total


def normalize_review(payload: dict[str, Any]) -> HostawayReview:
    """Convert one untrusted Hostaway review into validated application data."""
    review_id = _positive_int(payload.get("id"), "id")
    listing_map_id = _positive_int(payload.get("listingMapId"), "listingMapId")

    return HostawayReview(
        hostaway_review_id=review_id,
        hostaway_listing_map_id=listing_map_id,
        hostaway_reservation_id=_string(payload.get("reservationId")),
        external_review_id=_string(payload.get("externalReviewId")),
        channel_id=_string(payload.get("channelId")),
        review_type=_string(payload.get("type")),
        status=_string(payload.get("status")),
        guest_name=_string(payload.get("guestName")),
        rating=_decimal(payload.get("rating")),
        public_review=_string(payload.get("publicReview")).strip(),
        reviewee_response=_string(payload.get("revieweeResponse")).strip(),
        arrival_date=_date(payload.get("arrivalDate"), "arrivalDate"),
        departure_date=_date(payload.get("departureDate"), "departureDate"),
        source_updated_at=_datetime(
            payload.get("updatedOn", payload.get("updatedAt")),
            "updatedOn",
        ),
    )


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str | int):
        return str(value)
    raise HostawayResponseError("Hostaway review contains an invalid string field.")


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise HostawayResponseError(f"Hostaway review {field_name} is invalid.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HostawayResponseError(f"Hostaway review {field_name} must be an integer.") from exc
    if parsed <= 0:
        raise HostawayResponseError(f"Hostaway review {field_name} must be positive.")
    return parsed


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HostawayResponseError("Hostaway review rating is invalid.") from exc
    if parsed < 0 or parsed > 10:
        raise HostawayResponseError("Hostaway review rating must be between 0 and 10.")
    return parsed.quantize(Decimal("0.1"))


def _date(value: Any, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise HostawayResponseError(f"Hostaway review {field_name} is invalid.")
    parsed = parse_date(value[:10])
    if parsed is None:
        raise HostawayResponseError(f"Hostaway review {field_name} is invalid.")
    return parsed


def _datetime(value: Any, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise HostawayResponseError(f"Hostaway review {field_name} is invalid.")
    parsed = parse_datetime(value)
    if parsed is None:
        raise HostawayResponseError(f"Hostaway review {field_name} is invalid.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
    return parsed
