import httpx
import pytest

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import (
    HostawayAuthenticationError,
    HostawayTimeoutError,
)


def test_listing_timeout_is_handled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.hostaway.com/v1") as http:
        client = HostawayClient(access_token="test-token", client=http)
        with pytest.raises(HostawayTimeoutError):
            client.get_listings()


def test_listing_401_is_handled() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(401, request=request))
    with httpx.Client(transport=transport, base_url="https://api.hostaway.com/v1") as http:
        client = HostawayClient(access_token="test-token", client=http)
        with pytest.raises(HostawayAuthenticationError):
            client.get_listings()


def test_listing_429_has_bounded_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(
            200,
            json={"status": "success", "result": [], "page": 1, "totalPages": 1},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.hostaway.com/v1") as http:
        client = HostawayClient(
            access_token="test-token",
            client=http,
            sleeper=lambda seconds: None,
            max_get_attempts=3,
        )
        records, _, _ = client.get_listings()

    assert records == []
    assert attempts == 3
