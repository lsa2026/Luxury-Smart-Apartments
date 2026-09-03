"""Validated DTOs for Hostaway calendar and price details responses."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

from .exceptions import HostawayAvailabilityError, HostawayResponseError


@dataclass(frozen=True, slots=True)
class CalendarDay:
    date: date
    is_available: bool | None
    price: Decimal | None
    minimum_stay: int | None
    maximum_stay: int | None
    closed_on_arrival: bool | None
    closed_on_departure: bool | None
    status: str
    count_available_units: int | None = None
    available_units_to_sell: int | None = None
    count_reserved_units: int | None = None
    count_pending_units: int | None = None
    count_blocked_units: int | None = None
    count_blocking_reservations: int | None = None
    desired_units_to_sell: int | None = None
    is_processed: bool | None = None
    has_reservation_resources: bool = False


@dataclass(frozen=True, slots=True)
class CalendarDocument:
    days: tuple[CalendarDay, ...]
    envelope_fields: frozenset[str]
    day_field_types: tuple[tuple[str, str], ...]
    has_reservation_resources: bool = False


@dataclass(frozen=True, slots=True)
class PriceComponent:
    listing_fee_setting_id: int | None
    type: str
    name: str
    title: str
    alias: str
    quantity: int | None
    value: Decimal
    total: Decimal | None
    is_included_in_total: bool | None
    is_overridden_by_user: bool | None = None
    is_mandatory: bool | None = None
    is_deleted: bool | None = None


@dataclass(frozen=True, slots=True)
class PriceQuote:
    listing_id: int
    check_in: date
    check_out: date
    nights: int
    guests: int
    currency: str
    total_price: Decimal
    components: tuple[PriceComponent, ...]
    calculated_at: datetime
    envelope_fields: frozenset[str]
    result_field_types: tuple[tuple[str, str], ...]
    component_field_types: tuple[tuple[str, str], ...]


def validate_calendar_response(payload: Any) -> CalendarDocument:
    """Validate a read-only calendar response without retaining its raw body."""
    if not isinstance(payload, dict) or payload.get("status") not in (None, "success"):
        raise HostawayResponseError("Hostaway calendar response reports a failure.")
    result = payload.get("result")
    if not isinstance(result, list):
        raise HostawayResponseError("Hostaway calendar result must be an array.")

    days: list[CalendarDay] = []
    observed_types: dict[str, set[str]] = {}
    seen_dates: set[date] = set()
    has_reservation_resources = False
    for index, item in enumerate(result):
        if not isinstance(item, dict):
            raise HostawayResponseError(f"Hostaway calendar day {index} must be an object.")
        day_has_reservation_resources = "reservations" in item
        has_reservation_resources |= day_has_reservation_resources
        safe_item = {
            key: value for key, value in item.items() if key not in {"note", "reservations"}
        }
        _record_types(observed_types, safe_item)
        parsed_date = _date(item.get("date"), f"result[{index}].date")
        if parsed_date in seen_dates:
            raise HostawayResponseError("Hostaway calendar contains duplicate dates.")
        seen_dates.add(parsed_date)
        days.append(
            CalendarDay(
                date=parsed_date,
                is_available=_optional_bool(
                    item.get("isAvailable"),
                    f"result[{index}].isAvailable",
                ),
                price=_optional_decimal(item.get("price"), f"result[{index}].price"),
                minimum_stay=_optional_positive_int(
                    item.get("minimumStay"),
                    f"result[{index}].minimumStay",
                ),
                maximum_stay=_optional_positive_int(
                    item.get("maximumStay"),
                    f"result[{index}].maximumStay",
                ),
                closed_on_arrival=_optional_bool(
                    item.get("closedOnArrival"),
                    f"result[{index}].closedOnArrival",
                ),
                closed_on_departure=_optional_bool(
                    item.get("closedOnDeparture"),
                    f"result[{index}].closedOnDeparture",
                ),
                count_available_units=_optional_nonnegative_int(
                    item.get("countAvailableUnits"),
                    f"result[{index}].countAvailableUnits",
                ),
                available_units_to_sell=_optional_nonnegative_int(
                    _first_present(item, "availableUnitsToSell", "availableUnits"),
                    f"result[{index}].availableUnitsToSell",
                ),
                count_reserved_units=_optional_nonnegative_int(
                    item.get("countReservedUnits"),
                    f"result[{index}].countReservedUnits",
                ),
                count_pending_units=_optional_nonnegative_int(
                    item.get("countPendingUnits"),
                    f"result[{index}].countPendingUnits",
                ),
                count_blocked_units=_optional_nonnegative_int(
                    item.get("countBlockedUnits"),
                    f"result[{index}].countBlockedUnits",
                ),
                count_blocking_reservations=_optional_nonnegative_int(
                    item.get("countBlockingReservations"),
                    f"result[{index}].countBlockingReservations",
                ),
                desired_units_to_sell=_optional_nonnegative_int(
                    item.get("desiredUnitsToSell"),
                    f"result[{index}].desiredUnitsToSell",
                ),
                is_processed=_optional_bool(
                    item.get("isProcessed"),
                    f"result[{index}].isProcessed",
                ),
                status=_optional_string(item.get("status")),
                has_reservation_resources=day_has_reservation_resources,
            )
        )

    return CalendarDocument(
        days=tuple(days),
        envelope_fields=frozenset(payload),
        day_field_types=_flatten_types(observed_types),
        has_reservation_resources=has_reservation_resources,
    )


def validate_price_response(
    payload: Any,
    *,
    listing_id: int,
    check_in: date,
    check_out: date,
    guests: int,
    fallback_currency: str = "",
) -> PriceQuote:
    """Validate priceDetails v2 and convert all monetary values to Decimal."""
    if not isinstance(payload, dict):
        raise HostawayResponseError("Hostaway price response must be an object.")
    if payload.get("status") not in (None, "success"):
        raise HostawayAvailabilityError(
            "Hostaway could not calculate this stay under the current restrictions."
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise HostawayResponseError("Hostaway price result must be an object.")

    total_price = _required_decimal(result.get("totalPrice"), "result.totalPrice")
    if total_price < 0:
        raise HostawayResponseError("Hostaway totalPrice cannot be negative.")
    raw_components = result.get("components")
    if not isinstance(raw_components, list):
        raise HostawayResponseError("Hostaway price components must be an array.")

    components: list[PriceComponent] = []
    component_types: dict[str, set[str]] = {}
    for index, item in enumerate(raw_components):
        if not isinstance(item, dict):
            raise HostawayResponseError(f"Hostaway price component {index} must be an object.")
        _record_types(component_types, item)
        components.append(
            PriceComponent(
                listing_fee_setting_id=_optional_nonnegative_int(
                    item.get("listingFeeSettingId"),
                    f"components[{index}].listingFeeSettingId",
                ),
                type=_required_string(item.get("type"), f"components[{index}].type"),
                name=_required_string(item.get("name"), f"components[{index}].name"),
                title=_optional_string(item.get("title")),
                alias=_optional_string(item.get("alias")),
                quantity=_optional_nonnegative_int(
                    item.get("quantity"),
                    f"components[{index}].quantity",
                ),
                value=_required_decimal(item.get("value"), f"components[{index}].value"),
                total=_required_decimal(item.get("total"), f"components[{index}].total"),
                is_included_in_total=_required_bool(
                    item.get("isIncludedInTotalPrice"),
                    f"components[{index}].isIncludedInTotalPrice",
                ),
                is_overridden_by_user=_optional_bool(
                    item.get("isOverriddenByUser"),
                    f"components[{index}].isOverriddenByUser",
                ),
                is_mandatory=_optional_bool(
                    item.get("isMandatory"),
                    f"components[{index}].isMandatory",
                ),
                is_deleted=_required_bool(
                    item.get("isDeleted"),
                    f"components[{index}].isDeleted",
                ),
            )
        )

    response_currency = result.get("currency") or result.get("currencyCode")
    currency = _currency(response_currency or fallback_currency)
    if not currency:
        raise HostawayResponseError("Hostaway price response does not identify a currency.")

    result_types: dict[str, set[str]] = {}
    _record_types(result_types, result)
    return PriceQuote(
        listing_id=listing_id,
        check_in=check_in,
        check_out=check_out,
        nights=(check_out - check_in).days,
        guests=guests,
        currency=currency,
        total_price=total_price,
        components=tuple(components),
        calculated_at=timezone.now(),
        envelope_fields=frozenset(payload),
        result_field_types=_flatten_types(result_types),
        component_field_types=_flatten_types(component_types),
    )


def _record_types(target: dict[str, set[str]], payload: dict[str, Any]) -> None:
    for key, value in payload.items():
        target.setdefault(key, set()).add(_type_name(value))


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _flatten_types(values: dict[str, set[str]]) -> tuple[tuple[str, str], ...]:
    return tuple((key, "|".join(sorted(type_names))) for key, type_names in sorted(values.items()))


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise HostawayResponseError(f"{field_name} must be a YYYY-MM-DD string.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HostawayResponseError(f"{field_name} must be a valid date.") from exc


def _optional_bool(value: Any, field_name: str) -> bool | None:
    if value in (None, ""):
        return None
    if value is True or value == 1 or value == "1":
        return True
    if value is False or value == 0 or value == "0":
        return False
    raise HostawayResponseError(f"{field_name} must be boolean or 0/1.")


def _required_bool(value: Any, field_name: str) -> bool:
    parsed = _optional_bool(value, field_name)
    if parsed is None:
        raise HostawayResponseError(f"{field_name} is required.")
    return parsed


def _optional_nonnegative_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    parsed = _integer(value, field_name)
    if parsed < 0:
        raise HostawayResponseError(f"{field_name} cannot be negative.")
    return parsed


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    parsed = _integer(value, field_name)
    if parsed <= 0:
        raise HostawayResponseError(f"{field_name} must be positive.")
    return parsed


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise HostawayResponseError(f"{field_name} must be an integer.")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HostawayResponseError(f"{field_name} must be an integer.") from exc
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise HostawayResponseError(f"{field_name} must be an integer.")
    return int(parsed)


def _required_decimal(value: Any, field_name: str) -> Decimal:
    parsed = _optional_decimal(value, field_name)
    if parsed is None:
        raise HostawayResponseError(f"{field_name} is required.")
    return parsed


def _optional_decimal(value: Any, field_name: str) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise HostawayResponseError(f"{field_name} must be numeric.")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HostawayResponseError(f"{field_name} must be numeric.") from exc
    if not parsed.is_finite():
        raise HostawayResponseError(f"{field_name} must be numeric.")
    return parsed


def _required_string(value: Any, field_name: str) -> str:
    parsed = _optional_string(value)
    if not parsed:
        raise HostawayResponseError(f"{field_name} is required.")
    return parsed


def _optional_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _currency(value: Any) -> str:
    parsed = _optional_string(value).upper()
    if not parsed:
        return ""
    if len(parsed) != 3 or not parsed.isascii() or not parsed.isalpha():
        raise HostawayResponseError("Hostaway currency must be a three-letter code.")
    return parsed
