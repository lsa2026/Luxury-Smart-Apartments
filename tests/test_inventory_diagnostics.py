import logging
from datetime import date
from decimal import Decimal

from apps.integrations.hostaway.availability_validators import (
    CalendarDay,
    validate_calendar_response,
)
from apps.reservations.services.availability import (
    INVENTORY_CONFLICT,
    classify_inventory,
    resolve_day_inventory,
)


def day(**changes: object) -> CalendarDay:
    values = {
        "date": date(2030, 1, 1),
        "is_available": True,
        "price": Decimal("100"),
        "minimum_stay": 1,
        "maximum_stay": 30,
        "closed_on_arrival": False,
        "closed_on_departure": False,
        "status": "available",
    }
    values.update(changes)
    return CalendarDay(**values)


def test_single_unit_available_uses_is_available() -> None:
    decision = resolve_day_inventory(day(is_available=True))
    assert decision.is_available is True
    assert decision.strategy == "is_available"
    assert classify_inventory((day(),)) == "single_unit"


def test_single_unit_unavailable_uses_is_available() -> None:
    decision = resolve_day_inventory(day(is_available=False))
    assert decision.is_available is False
    assert decision.has_conflict is False


def test_multi_unit_available_units_to_sell_positive() -> None:
    decision = resolve_day_inventory(day(is_available=True, available_units_to_sell=2))
    assert decision.is_available is True
    assert decision.strategy == "available_units_to_sell"
    assert decision.is_multi_unit is True


def test_multi_unit_available_units_to_sell_zero() -> None:
    decision = resolve_day_inventory(day(is_available=False, available_units_to_sell=0))
    assert decision.is_available is False
    assert decision.strategy == "available_units_to_sell"


def test_multi_unit_falls_back_to_count_available_units() -> None:
    decision = resolve_day_inventory(day(is_available=True, count_available_units=3))
    assert decision.is_available is True
    assert decision.strategy == "count_available_units"


def test_multi_unit_falls_back_to_desired_units_to_sell() -> None:
    decision = resolve_day_inventory(day(is_available=True, desired_units_to_sell=1))
    assert decision.is_available is True
    assert decision.strategy == "desired_units_to_sell"


def test_conflict_is_available_zero_but_sellable_positive() -> None:
    decision = resolve_day_inventory(day(is_available=False, available_units_to_sell=1))
    assert decision.is_available is False
    assert decision.has_conflict is True
    assert decision.strategy == INVENTORY_CONFLICT


def test_conflict_is_available_one_but_sellable_zero() -> None:
    decision = resolve_day_inventory(day(is_available=True, available_units_to_sell=0))
    assert decision.is_available is False
    assert decision.has_conflict is True


def test_reserved_count_alone_does_not_prove_availability() -> None:
    decision = resolve_day_inventory(day(is_available=True, count_reserved_units=1))
    assert decision.is_multi_unit is True
    assert decision.is_available is False
    assert decision.strategy == "inventory_unconfirmed"


def test_reservation_payload_is_discarded_before_dto_and_schema(
    caplog: object,
) -> None:
    marker = "synthetic-private-reservation-marker"
    with caplog.at_level(logging.DEBUG):
        document = validate_calendar_response(
            {
                "status": "success",
                "result": [
                    {
                        "date": "2030-01-01",
                        "isAvailable": 0,
                        "note": marker,
                        "reservations": [
                            {
                                "reservationId": marker,
                                "guestName": marker,
                            }
                        ],
                    }
                ],
            }
        )
    assert document.has_reservation_resources is True
    assert document.days[0].has_reservation_resources is True
    assert "reservations" not in dict(document.day_field_types)
    assert "note" not in dict(document.day_field_types)
    assert marker not in repr(document)
    assert marker not in caplog.text
