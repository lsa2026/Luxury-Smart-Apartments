import httpx
import pytest

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import (
    HostawayAuthenticationError,
    HostawayTimeoutError,
)


def test_timeout_is_converted_to_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.hostaway.com/v1") as http:
        client = HostawayClient(access_token="test-token", client=http)
        with pytest.raises(HostawayTimeoutError, match="timed out"):
            client.get_reviews()


def test_401_is_converted_to_authentication_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(401, request=request))
    with httpx.Client(transport=transport, base_url="https://api.hostaway.com/v1") as http:
        client = HostawayClient(access_token="test-token", client=http)
        with pytest.raises(HostawayAuthenticationError, match="401"):
            client.get_reviews()


def test_429_retries_are_limited() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"result": [], "count": 0}, request=request)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.hostaway.com/v1") as http:
        client = HostawayClient(
            access_token="test-token",
            client=http,
            sleeper=sleeps.append,
            max_get_attempts=3,
        )
        records, total = client.get_reviews()

    assert records == []
    assert total is None
    assert attempts == 3
    assert sleeps == [0.0, 0.0]
