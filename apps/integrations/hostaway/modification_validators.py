"""Sanitized reservation observations and future modification request DTOs."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .exceptions import HostawayResponseError
from .reservation_validators import ReservationFinanceField


@dataclass(frozen=True, slots=True)
class ReservationIdentifierObservation:
    masked_reservation_id: str
    reservation_id: int
    listing_map_id: int | None
    channel_id: int | None
    channel_name: str
    source: str
    status: str
    payment_status: str
    field_types: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ReservationObservationDocument:
    observations: tuple[ReservationIdentifierObservation, ...]
    envelope_field_types: tuple[tuple[str, str], ...]
    count: int


@dataclass(frozen=True, slots=True)
class HostawayReservationUpdateRequest:
    """Minimal documented update fields from trusted current state."""

    listing_map_id: int
    check_in: date
    check_out: date
    guests: int
    currency: str
    total_price: Decimal
    finance_fields: tuple[ReservationFinanceField, ...]

    def to_payload(self) -> dict[str, Any]:
        if self.listing_map_id <= 0:
            raise ValueError("listing_map_id_not_verified")
        if self.check_out <= self.check_in:
            raise ValueError("invalid_dates")
        if self.guests <= 0:
            raise ValueError("invalid_guests")
        if not self.finance_fields:
            raise ValueError("price_components_missing")
        return {
            "listingMapId": self.listing_map_id,
            "arrivalDate": self.check_in.isoformat(),
            "departureDate": self.check_out.isoformat(),
            "numberOfGuests": self.guests,
            "adults": self.guests,
            "totalPrice": _json_number(self.total_price),
            "currency": self.currency.upper(),
            "financeField": [item.to_payload() for item in self.finance_fields],
        }


@dataclass(frozen=True, slots=True)
class HostawayReservationCancellationRequest:
    cancelled_by: str = "guest"

    def to_payload(self) -> dict[str, str]:
        if self.cancelled_by not in {"guest", "host"}:
            raise ValueError("invalid_cancellation_actor")
        return {"cancelledBy": self.cancelled_by}


def validate_reservation_observations(
    payload: Any,
    *,
    limit: int,
) -> ReservationObservationDocument:
    if not isinstance(payload, dict) or payload.get("status") not in (None, "success"):
        raise HostawayResponseError("Hostaway reservations response reports failure.")
    result = payload.get("result")
    if not isinstance(result, list):
        raise HostawayResponseError("Hostaway reservations result must be an array.")
    observations: list[ReservationIdentifierObservation] = []
    for item in result[:limit]:
        if not isinstance(item, dict):
            continue
        reservation_id = _optional_positive_int(item.get("hostawayReservationId") or item.get("id"))
        if reservation_id is None:
            continue
        observations.append(
            ReservationIdentifierObservation(
                masked_reservation_id=f"****{str(reservation_id)[-4:]}",
                reservation_id=reservation_id,
                listing_map_id=_optional_positive_int(item.get("listingMapId")),
                channel_id=_optional_positive_int(item.get("channelId")),
                channel_name=_safe_text(item.get("channelName"), 100),
                source=_safe_text(item.get("source"), 100),
                status=_safe_text(item.get("status"), 100),
                payment_status=_safe_text(item.get("paymentStatus"), 100),
                field_types=tuple(
                    sorted((str(key), type(value).__name__) for key, value in item.items())
                ),
            )
        )
    return ReservationObservationDocument(
        observations=tuple(observations),
        envelope_field_types=tuple(
            sorted((str(key), type(value).__name__) for key, value in payload.items())
        ),
        count=len(observations),
    )


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


def _json_number(value: Decimal) -> int | float:
    if not value.is_finite():
        raise ValueError("financial_value_not_finite")
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(format(value, "f"))
