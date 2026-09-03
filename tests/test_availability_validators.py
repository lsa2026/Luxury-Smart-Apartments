from datetime import date
from decimal import Decimal

import pytest

from apps.integrations.hostaway.availability_validators import (
    validate_calendar_response,
    validate_price_response,
)
from apps.integrations.hostaway.exceptions import HostawayResponseError


def test_partial_calendar_fields_are_optional() -> None:
    document = validate_calendar_response({"status": "success", "result": [{"date": "2030-02-01"}]})
    day = document.days[0]
    assert day.is_available is None
    assert day.price is None
    assert day.available_units_to_sell is None
    assert day.count_available_units is None


def test_invalid_calendar_boolean_is_rejected() -> None:
    with pytest.raises(HostawayResponseError):
        validate_calendar_response(
            {
                "status": "success",
                "result": [{"date": "2030-02-01", "isAvailable": "yes"}],
            }
        )


def test_price_currency_uses_valid_fallback_when_response_omits_it() -> None:
    quote = validate_price_response(
        {
            "status": "success",
            "result": {"totalPrice": 200, "components": []},
        },
        listing_id=1,
        check_in=date(2030, 2, 1),
        check_out=date(2030, 2, 3),
        guests=2,
        fallback_currency="sar",
    )
    assert quote.currency == "SAR"
    assert quote.total_price == Decimal("200")


def test_invalid_currency_is_rejected() -> None:
    with pytest.raises(HostawayResponseError):
        validate_price_response(
            {
                "status": "success",
                "result": {
                    "totalPrice": 200,
                    "currency": "SAR<script>",
                    "components": [],
                },
            },
            listing_id=1,
            check_in=date(2030, 2, 1),
            check_out=date(2030, 2, 3),
            guests=2,
        )


@pytest.mark.parametrize(
    "missing_field",
    ["total", "isIncludedInTotalPrice", "isDeleted"],
)
def test_price_components_require_authoritative_total_flags(
    missing_field: str,
) -> None:
    component = {
        "type": "fee",
        "name": "cleaningFee",
        "title": "Cleaning fee",
        "value": "100.00",
        "total": "200.00",
        "isIncludedInTotalPrice": 1,
        "isDeleted": 0,
    }
    component.pop(missing_field)

    with pytest.raises(HostawayResponseError):
        validate_price_response(
            {
                "status": "success",
                "result": {
                    "totalPrice": "200.00",
                    "currency": "SAR",
                    "components": [component],
                },
            },
            listing_id=1,
            check_in=date(2030, 2, 1),
            check_out=date(2030, 2, 3),
            guests=2,
        )


def test_unknown_fields_are_schema_only_not_retained_as_values() -> None:
    secret_marker = "synthetic-raw-marker"
    quote = validate_price_response(
        {
            "status": "success",
            "result": {
                "totalPrice": "100.00",
                "currency": "SAR",
                "components": [],
                "unexpected": secret_marker,
            },
        },
        listing_id=1,
        check_in=date(2030, 2, 1),
        check_out=date(2030, 2, 2),
        guests=1,
    )
    assert "unexpected" in dict(quote.result_field_types)
    assert secret_marker not in repr(quote)
