"""Secure acquisition and caching of Hostaway OAuth access tokens."""

import hashlib
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from django.conf import settings
from django.core.cache import caches

from .exceptions import (
    HostawayAuthenticationError,
    HostawayConfigurationError,
    HostawayNetworkError,
    HostawayRateLimitError,
    HostawayResponseError,
    HostawayServerError,
    HostawayTimeoutError,
)


class TokenCache(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any, timeout: int | None = None) -> None: ...

    def delete(self, key: str) -> bool: ...


class HostawayTokenProvider:
    """Resolve a configured token or create and cache one with client credentials."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        account_id: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        cache_backend: TokenCache | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        timeout: float | None = None,
    ) -> None:
        self._configured_token = (
            settings.HOSTAWAY_ACCESS_TOKEN if access_token is None else access_token
        ).strip()
        self.account_id = (
            settings.HOSTAWAY_ACCOUNT_ID if account_id is None else account_id
        ).strip()
        self._api_secret = (
            settings.HOSTAWAY_API_SECRET if api_secret is None else api_secret
        ).strip()
        self.base_url = (settings.HOSTAWAY_BASE_URL if base_url is None else base_url).rstrip("/")
        self._cache_alias = settings.HOSTAWAY_TOKEN_CACHE_ALIAS
        self._cache = cache_backend or caches[self._cache_alias]
        self._sleeper = sleeper
        self._configured_token_rejected = False
        self.last_source = ""
        self._owns_client = client is None
        self._client = client
        self._request_timeout = timeout or max(
            settings.HOSTAWAY_CONNECT_TIMEOUT,
            settings.HOSTAWAY_READ_TIMEOUT,
        )

        parsed_url = urlparse(self.base_url)
        if (
            parsed_url.scheme.lower() != "https"
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
        ):
            raise HostawayConfigurationError("Hostaway base URL must be a safe HTTPS URL.")

        if self._request_timeout <= 0:
            raise HostawayConfigurationError("Hostaway token timeout must be positive.")

    @property
    def masked_account_id(self) -> str:
        if not self.account_id:
            return "not-configured"
        return f"****{self.account_id[-4:]}"

    def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a token without exposing it through logs or exceptions."""
        if self._configured_token and not force_refresh and not self._configured_token_rejected:
            self.last_source = "environment"
            return self._configured_token

        if not force_refresh:
            cached_token = self._cache.get(self._cache_key())
            if isinstance(cached_token, str) and cached_token:
                self.last_source = "cache"
                return cached_token

        self._validate_generation_configuration()
        token, expires_in = self._create_token()
        safety = min(
            settings.HOSTAWAY_TOKEN_CACHE_SAFETY_SECONDS,
            max(1, expires_in // 10),
        )
        self._cache.set(
            self._cache_key(),
            token,
            timeout=max(1, expires_in - safety),
        )
        self._configured_token_rejected = True
        self.last_source = "generated"
        self._sleeper(1.0)
        return token

    def invalidate(self) -> None:
        """Forget the generated token after Hostaway returns HTTP 403."""
        self._configured_token_rejected = True
        if self.account_id:
            self._cache.delete(self._cache_key())

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def _validate_generation_configuration(self) -> None:
        if not self.account_id or not self._api_secret:
            raise HostawayConfigurationError(
                "HOSTAWAY_ACCOUNT_ID and HOSTAWAY_API_SECRET are required "
                "to generate an access token."
            )
        if settings.HOSTAWAY_REQUIRE_SHARED_TOKEN_CACHE and not self._uses_shared_cache():
            raise HostawayConfigurationError(
                "Production token generation requires a shared Redis or Memcached "
                "Django cache, or a configured HOSTAWAY_ACCESS_TOKEN."
            )

    def _uses_shared_cache(self) -> bool:
        backend = settings.CACHES[self._cache_alias]["BACKEND"].casefold()
        return "redis" in backend or "memcached" in backend

    def _cache_key(self) -> str:
        identity = self.account_id or "unconfigured"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return f"hostaway:access-token:{digest}"

    def _create_token(self) -> tuple[str, int]:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(self._request_timeout),
                headers={
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                    "User-Agent": "LuxurySmartApartments/0.3 (+hostaway-verification)",
                },
            )
        try:
            response = self._client.post(
                "/accessTokens",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.account_id,
                    "client_secret": self._api_secret,
                    "scope": "general",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as exc:
            raise HostawayTimeoutError("Hostaway token request timed out.") from exc
        except httpx.NetworkError as exc:
            raise HostawayNetworkError("Hostaway token network request failed.") from exc

        if response.status_code in (401, 403):
            raise HostawayAuthenticationError("Hostaway rejected the token credentials.")
        if response.status_code == 429:
            raise HostawayRateLimitError("Hostaway rate-limited the token request.")
        if response.status_code >= 500:
            raise HostawayServerError("Hostaway token service returned a server error.")
        if response.is_error:
            raise HostawayResponseError(
                f"Hostaway token service returned HTTP {response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise HostawayResponseError("Hostaway token response is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise HostawayResponseError("Hostaway token response must be an object.")
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        token_type = payload.get("token_type")
        if not isinstance(token, str) or not token:
            raise HostawayResponseError("Hostaway token response is missing access_token.")
        if token_type not in (None, "Bearer"):
            raise HostawayResponseError("Hostaway token response has an unsupported token type.")
        if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
            raise HostawayResponseError("Hostaway token response has an invalid expiry.")
        return token, expires_in
