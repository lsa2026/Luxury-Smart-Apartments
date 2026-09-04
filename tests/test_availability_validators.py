from datetime import date
from unittest.mock import patch

import pytest

from apps.integrations.hostaway.availability_validators import (
    resolve_hostaway_price_currency,
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


def test_price_currency_must_be_explicit_in_current_response() -> None:
    with pytest.raises(HostawayResponseError, match="does not identify"):
        validate_price_response(
            {
                "status": "success",
                "result": {"totalPrice": 200, "components": []},
            },
            listing_id=1,
            check_in=date(2030, 2, 1),
            check_out=date(2030, 2, 3),
            guests=2,
        )


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


def _price_currency_payload(currency: str | None) -> dict:
    result = {"totalPrice": "200.00", "components": []}
    if currency is not None:
        result["currency"] = currency
    return {"status": "success", "result": result}


@pytest.mark.parametrize(
    ("price_currency", "listing_currency", "expected"),
    [
        ("MAD", "MAD", "MAD"),
        (None, "MAD", "MAD"),
        (None, "SAR", "SAR"),
        ("SAR", "SAR", "SAR"),
    ],
)
def test_hostaway_currency_precedence_accepts_matching_authoritative_sources(
    price_currency: str | None,
    listing_currency: str,
    expected: str,
) -> None:
    assert (
        resolve_hostaway_price_currency(
            511786,
            _price_currency_payload(price_currency),
            {"id": 511786, "currencyCode": listing_currency},
        )
        == expected
    )


@pytest.mark.parametrize(
    ("price_currency", "listing_currency"),
    [("MAD", "SAR"), ("SAR", "MAD")],
)
def test_hostaway_currency_conflict_fails_closed_and_is_logged(
    price_currency: str,
    listing_currency: str,
) -> None:
    with patch("apps.integrations.hostaway.availability_validators.logger.error") as log_error:
        with pytest.raises(HostawayResponseError, match="conflict"):
            resolve_hostaway_price_currency(
                511786,
                _price_currency_payload(price_currency),
                {"id": 511786, "currencyCode": listing_currency},
            )
    assert log_error.call_args.args[0].startswith("HOSTAWAY_CURRENCY_CONFLICT")


def test_hostaway_currency_missing_from_both_sources_is_rejected() -> None:
    with pytest.raises(HostawayResponseError, match="does not identify"):
        resolve_hostaway_price_currency(
            511786,
            _price_currency_payload(None),
            {"id": 511786, "currencyCode": None},
        )


def test_hostaway_currency_rejects_wrong_listing_binding() -> None:
    with pytest.raises(HostawayResponseError, match="different listing ID"):
        resolve_hostaway_price_currency(
            511786,
            _price_currency_payload(None),
            {"id": 315814, "currencyCode": "SAR"},
        )


def test_hostaway_currency_rejects_unsupported_listing_currency() -> None:
    with pytest.raises(HostawayResponseError, match="unsupported"):
        resolve_hostaway_price_currency(
            511786,
            _price_currency_payload(None),
            {"id": 511786, "currencyCode": "GBP"},
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
