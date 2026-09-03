from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import (
    HostawayAuthenticationError,
    HostawayRateLimitError,
    HostawayServerError,
    HostawayTimeoutError,
)


class StubTokenProvider:
    masked_account_id = "****1234"

    def __init__(self) -> None:
        self.invalidations = 0
        self.refreshes = 0

    def get_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self.refreshes += 1
        return "refreshed-token" if force_refresh else "initial-token"

    def invalidate(self) -> None:
        self.invalidations += 1

    def close(self) -> None:
        pass


def calendar_payload() -> dict[str, Any]:
    return {
        "status": "success",
        "result": [
            {
                "date": "2030-01-01",
                "isAvailable": 1,
                "price": "123.45",
                "minimumStay": "2",
                "maximumStay": 30,
                "closedOnArrival": 0,
                "closedOnDeparture": None,
                "countAvailableUnits": "3",
                "availableUnitsToSell": "1",
                "countReservedUnits": 2,
                "countPendingUnits": 0,
                "countBlockedUnits": "0",
                "countBlockingReservations": 2,
                "desiredUnitsToSell": 1,
                "isProcessed": 1,
                "newField": "ignored",
            }
        ],
    }


def price_payload() -> dict[str, Any]:
    return {
        "status": "success",
        "result": {
            "totalPrice": "450.25",
            "currency": "SAR",
            "components": [
                {
                    "listingFeeSettingId": 5,
                    "type": "price",
                    "name": "baseRate",
                    "title": "Base rate",
                    "alias": None,
                    "quantity": "2",
                    "value": "400.20",
                    "total": "400.20",
                    "isIncludedInTotalPrice": 1,
                    "isDeleted": 0,
                }
            ],
            "unknown": {"safe": True},
        },
    }


def test_get_calendar_validates_and_sends_no_resources() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.query.decode()
        return httpx.Response(200, json=calendar_payload(), request=request)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.hostaway.com/v1") as http:
        client = HostawayClient(access_token="test", client=http)
        document = client.get_listing_calendar(
            100,
            start_date=date(2030, 1, 1),
            end_date=date(2030, 1, 2),
        )

    assert document.days[0].price == Decimal("123.45")
    assert document.days[0].is_available is True
    assert document.days[0].closed_on_arrival is False
    assert document.days[0].count_available_units == 3
    assert document.days[0].available_units_to_sell == 1
    assert document.days[0].count_reserved_units == 2
    assert document.days[0].count_pending_units == 0
    assert document.days[0].count_blocked_units == 0
    assert document.days[0].count_blocking_reservations == 2
    assert document.days[0].is_processed is True
    assert "includeResources=0" in captured["query"]
    assert "newField" in dict(document.day_field_types)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2030, 1, 2), date(2030, 1, 2)),
        (date(2030, 1, 3), date(2030, 1, 2)),
        (date(2030, 1, 1), date(2031, 1, 3)),
    ],
)
def test_calendar_rejects_invalid_ranges_before_http(start: date, end: date) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=calendar_payload(), request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(access_token="test", client=http)
        with pytest.raises(ValueError):
            client.get_listing_calendar(100, start_date=start, end_date=end)
    assert calls == 0


def test_calculate_price_uses_exact_version_two_body_and_decimal() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = request.read().decode()
        captured["content_type"] = request.headers["content-type"]
        return httpx.Response(200, json=price_payload(), request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(access_token="test", client=http)
        quote = client.calculate_price(
            100,
            check_in=date(2030, 1, 1),
            check_out=date(2030, 1, 3),
            guests=2,
        )

    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["body"] == (
        '{"startingDate":"2030-01-01","endingDate":"2030-01-03","numberOfGuests":2,"version":2}'
    )
    assert quote.total_price == Decimal("450.25")
    assert quote.components[0].value == Decimal("400.20")


def test_price_post_is_not_retried_on_500() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(access_token="test", client=http)
        with pytest.raises(HostawayServerError):
            client.calculate_price(
                100,
                check_in=date(2030, 1, 1),
                check_out=date(2030, 1, 3),
                guests=2,
                fallback_currency="SAR",
            )
    assert attempts == 1


def test_price_post_is_not_retried_on_429() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(access_token="test", client=http)
        with pytest.raises(HostawayRateLimitError):
            client.calculate_price(
                100,
                check_in=date(2030, 1, 1),
                check_out=date(2030, 1, 3),
                guests=2,
                fallback_currency="SAR",
            )
    assert attempts == 1


def test_calendar_get_retries_429_bounded() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json=calendar_payload(), request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(
            access_token="test",
            client=http,
            sleeper=lambda _: None,
            max_get_attempts=3,
        )
        client.get_listing_calendar(
            100,
            start_date=date(2030, 1, 1),
            end_date=date(2030, 1, 2),
        )
    assert attempts == 3


def test_calendar_get_retries_5xx_bounded() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=calendar_payload(), request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(
            access_token="test",
            client=http,
            sleeper=lambda _: None,
            max_get_attempts=3,
        )
        client.get_listing_calendar(
            100,
            start_date=date(2030, 1, 1),
            end_date=date(2030, 1, 2),
        )
    assert attempts == 3


def test_calendar_timeout_is_structured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(access_token="test", client=http)
        with pytest.raises(HostawayTimeoutError):
            client.get_listing_calendar(
                100,
                start_date=date(2030, 1, 1),
                end_date=date(2030, 1, 2),
            )


def test_price_timeout_is_structured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(access_token="test", client=http)
        with pytest.raises(HostawayTimeoutError):
            client.calculate_price(
                100,
                check_in=date(2030, 1, 1),
                check_out=date(2030, 1, 3),
                guests=2,
                fallback_currency="SAR",
            )


def test_401_is_not_refreshed() -> None:
    provider = StubTokenProvider()
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, request=request)),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(client=http, token_provider=provider)
        with pytest.raises(HostawayAuthenticationError):
            client.get_listing_calendar(
                100,
                start_date=date(2030, 1, 1),
                end_date=date(2030, 1, 2),
            )
    assert provider.refreshes == 0


def test_price_403_refreshes_once_without_loop() -> None:
    provider = StubTokenProvider()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, request=request)

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        client = HostawayClient(client=http, token_provider=provider)
        with pytest.raises(HostawayAuthenticationError):
            client.calculate_price(
                100,
                check_in=date(2030, 1, 1),
                check_out=date(2030, 1, 3),
                guests=2,
                fallback_currency="SAR",
            )
    assert attempts == 2
    assert provider.invalidations == 1
    assert provider.refreshes == 1
