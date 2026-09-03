"""Minimal server-side HTTP client for HyperPay COPYandPAY."""

from collections.abc import Mapping
from typing import Any

import httpx
from django.conf import settings

from .exceptions import (
    HyperPayAuthenticationError,
    HyperPayConfigurationError,
    HyperPayConnectionError,
    HyperPayResponseError,
)


class HyperPayClient:
    def __init__(self, http: httpx.Client | None = None) -> None:
        if not settings.HYPERPAY_ENABLED:
            raise HyperPayConfigurationError()
        if not settings.HYPERPAY_ENTITY_ID or not settings.HYPERPAY_ACCESS_TOKEN:
            raise HyperPayConfigurationError()
        self._owns_http = http is None
        self.http = http or httpx.Client(
            base_url=settings.HYPERPAY_BASE_URL,
            timeout=httpx.Timeout(
                settings.HYPERPAY_READ_TIMEOUT,
                connect=settings.HYPERPAY_CONNECT_TIMEOUT,
            ),
            headers={
                "Authorization": f"Bearer {settings.HYPERPAY_ACCESS_TOKEN}",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> "HyperPayClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def create_checkout(self, payload: Mapping[str, str]) -> dict[str, Any]:
        return self._request("POST", "/v1/checkouts", data=payload)

    def get_checkout_payment(self, checkout_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/checkouts/{checkout_id}/payment",
            params={"entityId": settings.HYPERPAY_ENTITY_ID},
        )

    def _request(self, method: str, path: str, **kwargs: object) -> dict[str, Any]:
        try:
            response = self.http.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise HyperPayConnectionError() from exc
        if response.status_code in {401, 403}:
            raise HyperPayAuthenticationError()
        try:
            document = response.json()
        except (ValueError, TypeError) as exc:
            raise HyperPayResponseError() from exc
        if not isinstance(document, dict):
            raise HyperPayResponseError()
        if response.status_code >= 500:
            raise HyperPayConnectionError()
        if response.status_code >= 400:
            raise HyperPayResponseError("hyperpay_request_rejected")
        return document
