"""Sanitized DTOs and validators for Hostaway reservation operations."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .availability_validators import PriceComponent
from .exceptions import HostawayResponseError


@dataclass(frozen=True, slots=True)
class ReservationFinanceField:
    listing_fee_setting_id: int | None
    type: str
    name: str
    title: str
    alias: str
    quantity: int | None
    value: Decimal
    total: Decimal
    is_included_in_total_price: bool
    is_overridden_by_user: bool
    is_mandatory: bool | None
    is_deleted: bool

    @classmethod
    def from_price_component(cls, component: PriceComponent) -> "ReservationFinanceField":
        if component.total is None:
            raise ValueError("price_component_total_missing")
        required_flags = (
            component.is_included_in_total,
            component.is_deleted,
        )
        if any(value is None for value in required_flags):
            raise ValueError("price_component_flags_missing")
        return cls(
            listing_fee_setting_id=component.listing_fee_setting_id,
            type=component.type,
            name=component.name,
            title=component.title,
            alias=component.alias,
            quantity=component.quantity,
            value=component.value,
            total=component.total,
            is_included_in_total_price=bool(component.is_included_in_total),
            is_overridden_by_user=False,
            # Hostaway's own price calculator legitimately returns null for
            # this field (including baseRate). Preserve that value verbatim;
            # inventing either true or false would change the quoted component.
            is_mandatory=component.is_mandatory,
            is_deleted=bool(component.is_deleted),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "name": self.name,
            "title": self.title,
            "alias": self.alias,
            "value": _json_number(self.value),
            "total": _json_number(self.total),
            "isIncludedInTotalPrice": int(self.is_included_in_total_price),
            "isOverriddenByUser": int(self.is_overridden_by_user),
            "isMandatory": (
                int(self.is_mandatory) if self.is_mandatory is not None else None
            ),
            "isDeleted": int(self.is_deleted),
        }
        if self.listing_fee_setting_id is not None:
            payload["listingFeeSettingId"] = self.listing_fee_setting_id
        if self.quantity is not None:
            payload["quantity"] = self.quantity
        return payload


@dataclass(frozen=True, slots=True)
class HostawayReservationCreateRequest:
    listing_map_id: int
    channel_id: int
    guest_first_name: str
    guest_last_name: str
    guest_email: str
    guest_phone: str
    guest_country_code: str
    guests: int
    check_in: date
    check_out: date
    currency: str
    total_price: Decimal
    finance_fields: tuple[ReservationFinanceField, ...]
    provider: str

    def to_payload(self) -> dict[str, Any]:
        if self.listing_map_id <= 0:
            raise ValueError("listing_map_id_not_verified")
        if self.channel_id <= 0:
            raise ValueError("direct_channel_id_not_configured")
        if not self.finance_fields:
            raise ValueError("price_components_missing")
        return {
            "channelId": self.channel_id,
            "listingMapId": self.listing_map_id,
            "isManuallyChecked": 0,
            "isInitial": 0,
            "guestName": f"{self.guest_first_name} {self.guest_last_name}".strip()[:200],
            "guestFirstName": self.guest_first_name[:100],
            "guestLastName": self.guest_last_name[:100],
            "guestCountry": self.guest_country_code.upper(),
            "guestEmail": self.guest_email,
            "phone": self.guest_phone,
            "numberOfGuests": self.guests,
            "adults": self.guests,
            "arrivalDate": self.check_in.isoformat(),
            "departureDate": self.check_out.isoformat(),
            "totalPrice": _json_number(self.total_price),
            "currency": self.currency.upper(),
            "financeField": [item.to_payload() for item in self.finance_fields],
        }


@dataclass(frozen=True, slots=True)
class HostawayReservationSnapshot:
    reservation_id: int
    listing_map_id: int
    channel_id: int | None
    status: str
    check_in: date
    check_out: date
    guests: int
    currency: str
    total_price: Decimal
    payment_status: str
    source: str
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class HostawayReservationCreateResult:
    snapshot: HostawayReservationSnapshot


def validate_reservation_response(payload: Any) -> HostawayReservationSnapshot:
    if not isinstance(payload, dict) or payload.get("status") not in (None, "success"):
        raise HostawayResponseError("Hostaway reservation response reports failure.")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise HostawayResponseError("Hostaway reservation result must be an object.")
    reservation_id = _positive_int(
        result.get("hostawayReservationId") or result.get("id"),
        "reservation.id",
    )
    listing_map_id = _positive_int(result.get("listingMapId"), "reservation.listingMapId")
    check_in = _required_date(result.get("arrivalDate"), "reservation.arrivalDate")
    check_out = _required_date(result.get("departureDate"), "reservation.departureDate")
    if check_out <= check_in:
        raise HostawayResponseError("Hostaway reservation dates are invalid.")
    currency = _currency(result.get("currency"))
    return HostawayReservationSnapshot(
        reservation_id=reservation_id,
        listing_map_id=listing_map_id,
        channel_id=_optional_positive_int(result.get("channelId"), "reservation.channelId"),
        status=_optional_text(result.get("status")),
        check_in=check_in,
        check_out=check_out,
        guests=_positive_int(
            result.get("numberOfGuests") or result.get("adults"),
            "reservation.numberOfGuests",
        ),
        currency=currency,
        total_price=_decimal(result.get("totalPrice"), "reservation.totalPrice"),
        payment_status=_optional_text(
            result.get("paymentStatus")
            or ("paid" if result.get("isPaid") in (1, True, "1") else "")
        ),
        source=_optional_text(result.get("source") or result.get("channelName")),
        updated_at=_optional_datetime(result.get("updatedOn") or result.get("latestActivityOn")),
    )


# Every status the Hostaway reservation filter can emit, keyed by its casefolded
# API value. Anything absent stays "unknown" so a status Hostaway adds later is
# surfaced for review instead of being silently treated as a booking.
HOSTAWAY_RESERVATION_STATUS_MAP = {
    # Active stays.
    "new": "confirmed",
    "confirmed": "confirmed",
    "ownerstay": "confirmed",
    "modified": "modified",
    # Booked but not yet settled.
    "awaitingpayment": "awaiting_payment",
    "pending": "pending",
    "unconfirmed": "pending",
    # Leads that were never a booking.
    "inquiry": "inquiry",
    "inquirypreapproved": "inquiry",
    # Closed without a stay.
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "declined": "declined",
    "inquirydenied": "declined",
    "inquirynotpossible": "declined",
    "expired": "expired",
    "inquirytimeout": "expired",
}


def normalize_hostaway_reservation_status(status: str) -> str:
    normalized = status.strip().casefold()
    return HOSTAWAY_RESERVATION_STATUS_MAP.get(normalized, "unknown")


def source_type_from_snapshot(snapshot: HostawayReservationSnapshot) -> str:
    source = snapshot.source.casefold()
    if source in {"manual", "hostaway", "hostawaymanual"}:
        return "hostaway_manual"
    if source in {"luxurysmartapartments", "direct", "bookingengine"}:
        return "direct_website"
    if source:
        return "external_channel"
    return "unknown"


def _json_number(value: Decimal) -> int | float:
    if not value.is_finite():
        raise ValueError("financial_value_not_finite")
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(format(value, "f"))


def _positive_int(value: Any, field: str) -> int:
    parsed = _optional_positive_int(value, field)
    if parsed is None:
        raise HostawayResponseError(f"{field} must be a positive integer.")
    return parsed


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise HostawayResponseError(f"{field} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HostawayResponseError(f"{field} must be an integer.") from exc
    if parsed <= 0:
        raise HostawayResponseError(f"{field} must be positive.")
    return parsed


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HostawayResponseError(f"{field} must be decimal.") from exc
    if not parsed.is_finite() or parsed < 0:
        raise HostawayResponseError(f"{field} must be a non-negative finite decimal.")
    return parsed


def _currency(value: Any) -> str:
    parsed = _optional_text(value).upper()
    if len(parsed) != 3 or not parsed.isascii() or not parsed.isalpha():
        raise HostawayResponseError("reservation.currency must be an ISO currency code.")
    return parsed


def _required_date(value: Any, field: str) -> date:
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise HostawayResponseError(f"{field} must be an ISO date.") from exc
    return parsed


def _optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    parsed = parse_datetime(str(value).replace(" ", "T", 1))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _optional_text(value: Any) -> str:
    return value.strip()[:100] if isinstance(value, str) else ""
