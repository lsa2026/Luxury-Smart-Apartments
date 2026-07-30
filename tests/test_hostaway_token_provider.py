import logging
from urllib.parse import parse_qs

import httpx
import pytest
from django.test import override_settings

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import (
    HostawayAuthenticationError,
    HostawayConfigurationError,
)
from apps.integrations.hostaway.token_provider import HostawayTokenProvider


class MemoryTokenCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.timeouts: list[int | None] = []

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set(self, key: str, value: str, timeout: int | None = None) -> None:
        self.values[key] = value
        self.timeouts.append(timeout)

    def delete(self, key: str) -> bool:
        return self.values.pop(key, None) is not None


@override_settings(HOSTAWAY_ACCESS_TOKEN="environment-token")
def test_uses_access_token_from_environment_without_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Token endpoint must not be called.")

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        provider = HostawayTokenProvider(client=http)
        assert provider.get_token() == "environment-token"
        assert provider.last_source == "environment"


def test_creates_client_credentials_token_as_form_and_caches_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    secret_token = "generated-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "expires_in": 3600,
                "access_token": secret_token,
            },
            request=request,
        )

    cache = MemoryTokenCache()
    caplog.set_level(logging.DEBUG)
    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        provider = HostawayTokenProvider(
            access_token="",
            account_id="12345",
            api_secret="api-secret",
            client=http,
            cache_backend=cache,
            sleeper=lambda seconds: None,
        )
        assert provider.get_token() == secret_token
        assert provider.get_token() == secret_token

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/accessTokens"
    assert requests[0].headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = parse_qs(requests[0].content.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "client_id": ["12345"],
        "client_secret": ["api-secret"],
        "scope": ["general"],
    }
    assert cache.timeouts == [3300]
    assert secret_token not in caplog.text


def test_cached_token_is_reused_across_provider_instances() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={"access_token": "cached-token", "expires_in": 600},
            request=request,
        )

    cache = MemoryTokenCache()
    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.hostaway.com/v1",
    ) as http:
        first = HostawayTokenProvider(
            access_token="",
            account_id="123",
            api_secret="secret",
            cache_backend=cache,
            client=http,
            sleeper=lambda seconds: None,
        )
        second = HostawayTokenProvider(
            access_token="",
            account_id="123",
            api_secret="secret",
            cache_backend=cache,
            client=http,
            sleeper=lambda seconds: None,
        )
        assert first.get_token() == "cached-token"
        assert second.get_token() == "cached-token"

    assert requests == 1
    assert second.last_source == "cache"


def test_http_403_refreshes_once_and_retries_get_once() -> None:
    token_requests = 0
    get_requests = 0

    def token_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        token_requests += 1
        return httpx.Response(
            200,
            json={"access_token": "fresh-token", "expires_in": 600},
            request=request,
        )

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_requests
        get_requests += 1
        if request.headers["Authorization"] == "Bearer stale-token":
            return httpx.Response(403, request=request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "result": [],
                "limit": 100,
                "offset": 0,
                "count": 0,
            },
            request=request,
        )

    cache = MemoryTokenCache()
    with (
        httpx.Client(
            transport=httpx.MockTransport(token_handler),
            base_url="https://api.hostaway.com/v1",
        ) as token_http,
        httpx.Client(
            transport=httpx.MockTransport(api_handler),
            base_url="https://api.hostaway.com/v1",
        ) as api_http,
    ):
        provider = HostawayTokenProvider(
            access_token="stale-token",
            account_id="123",
            api_secret="secret",
            cache_backend=cache,
            client=token_http,
            sleeper=lambda seconds: None,
        )
        client = HostawayClient(client=api_http, token_provider=provider)
        records, count = client.get_listings()

    assert records == []
    assert count == 0
    assert token_requests == 1
    assert get_requests == 2


def test_repeated_403_does_not_create_a_refresh_loop() -> None:
    token_requests = 0
    get_requests = 0

    def token_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        token_requests += 1
        return httpx.Response(
            200,
            json={"access_token": "still-rejected", "expires_in": 600},
            request=request,
        )

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_requests
        get_requests += 1
        return httpx.Response(403, request=request)

    with (
        httpx.Client(
            transport=httpx.MockTransport(token_handler),
            base_url="https://api.hostaway.com/v1",
        ) as token_http,
        httpx.Client(
            transport=httpx.MockTransport(api_handler),
            base_url="https://api.hostaway.com/v1",
        ) as api_http,
    ):
        provider = HostawayTokenProvider(
            access_token="stale-token",
            account_id="123",
            api_secret="secret",
            cache_backend=MemoryTokenCache(),
            client=token_http,
            sleeper=lambda seconds: None,
        )
        client = HostawayClient(client=api_http, token_provider=provider)
        with pytest.raises(HostawayAuthenticationError, match="403"):
            client.get_listings()

    assert token_requests == 1
    assert get_requests == 2


@override_settings(
    HOSTAWAY_REQUIRE_SHARED_TOKEN_CACHE=True,
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "token-provider-production-test",
        }
    },
)
def test_production_generation_rejects_process_local_cache() -> None:
    provider = HostawayTokenProvider(
        access_token="",
        account_id="123",
        api_secret="secret",
        cache_backend=MemoryTokenCache(),
    )

    with pytest.raises(HostawayConfigurationError, match="shared Redis or Memcached"):
        provider.get_token()
