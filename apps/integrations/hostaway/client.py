"""The sole HTTP boundary for Hostaway API calls."""

import logging
import time
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx
from django.conf import settings

from .availability_validators import (
    CalendarDocument,
    PriceQuote,
    validate_calendar_response,
    validate_price_response,
)
from .exceptions import (
    HostawayAuthenticationError,
    HostawayAvailabilityError,
    HostawayConfigurationError,
    HostawayNetworkError,
    HostawayNotFoundError,
    HostawayRateLimitError,
    HostawayResponseError,
    HostawayServerError,
    HostawayTimeoutError,
)
from .listing_validators import (
    HostawayCollectionPage,
    HostawayObjectDocument,
    inspect_collection_response,
    inspect_object_response,
    validate_collection_response,
)
from .token_provider import HostawayTokenProvider
from .validators import validate_reviews_page

logger = logging.getLogger(__name__)


class HostawayClient:
    """Authenticated, retry-bounded Hostaway API client."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        token_provider: HostawayTokenProvider | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_get_attempts: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.HOSTAWAY_BASE_URL).rstrip(
            "/"
        )
        self.sleeper = sleeper
        self.max_get_attempts = (
            max_get_attempts if max_get_attempts is not None else settings.HOSTAWAY_MAX_GET_ATTEMPTS
        )
        self._owns_client = client is None
        self._owns_token_provider = token_provider is None

        if urlparse(self.base_url).scheme.lower() != "https":
            raise HostawayConfigurationError("Hostaway base URL must use HTTPS.")
        if self.max_get_attempts < 1:
            raise HostawayConfigurationError("Hostaway GET attempts must be at least one.")

        self._token_provider = token_provider or HostawayTokenProvider(
            access_token=access_token,
            base_url=self.base_url,
            sleeper=sleeper,
            timeout=timeout,
        )
        http_timeout = httpx.Timeout(
            connect=timeout or settings.HOSTAWAY_CONNECT_TIMEOUT,
            read=timeout or settings.HOSTAWAY_READ_TIMEOUT,
            write=timeout or settings.HOSTAWAY_READ_TIMEOUT,
            pool=timeout or settings.HOSTAWAY_CONNECT_TIMEOUT,
        )
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=http_timeout,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "LuxurySmartApartments/0.3 (+hostaway-verification)",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        if self._owns_token_provider:
            self._token_provider.close()

    def __enter__(self) -> "HostawayClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def authenticate(self) -> None:
        """Resolve credentials without returning them to the caller."""
        self._token_provider.get_token()

    @property
    def masked_account_id(self) -> str:
        return self._token_provider.masked_account_id

    def get_reviews(
        self,
        *,
        listing_map_ids: Sequence[int] | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_by: str | None = None,
        sort_order: str | None = None,
        reservation_id: str | None = None,
        review_type: str | None = None,
        statuses: Sequence[str] | None = None,
        departure_date_start: str | None = None,
        departure_date_end: str | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Fetch and validate one page from GET /reviews."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        if offset < 0:
            raise ValueError("offset cannot be negative.")

        params: list[tuple[str, str | int]] = [
            ("limit", limit),
            ("offset", offset),
        ]
        optional_params: dict[str, str | None] = {
            "sortBy": sort_by,
            "sortOrder": sort_order,
            "reservationId": reservation_id,
            "type": review_type,
            "departureDateStart": departure_date_start,
            "departureDateEnd": departure_date_end,
        }
        params.extend((key, value) for key, value in optional_params.items() if value is not None)
        params.extend(
            (f"listingMapIds[{index}]", listing_id)
            for index, listing_id in enumerate(listing_map_ids or ())
        )
        params.extend((f"statuses[{index}]", status) for index, status in enumerate(statuses or ()))

        response = self._get_with_retry("/reviews", params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise HostawayResponseError("Hostaway returned invalid JSON.") from exc
        return validate_reviews_page(payload)

    def get_listings(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include_resources: bool = True,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Fetch one documented page from GET /listings."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        if offset < 0:
            raise ValueError("offset cannot be negative.")
        document = self.get_listings_page(
            limit=limit,
            offset=offset,
            include_resources=include_resources,
        )
        page = validate_collection_response(
            {
                "status": document.status,
                "result": list(document.records),
                "count": document.count,
            }
        )
        return page

    def get_listings_page(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include_resources: bool = True,
    ) -> HostawayCollectionPage:
        """Fetch a listing page while retaining the standard envelope metadata."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        if offset < 0:
            raise ValueError("offset cannot be negative.")
        payload = self._get_json(
            "/listings",
            params=[
                ("limit", limit),
                ("offset", offset),
                ("includeResources", int(include_resources)),
            ],
        )
        return inspect_collection_response(payload)

    def get_listing(
        self,
        listing_id: int,
        *,
        include_resources: bool = True,
    ) -> dict[str, Any]:
        """Fetch one listing and embedded resources from GET /listings/{id}."""
        return self.get_listing_document(
            listing_id,
            include_resources=include_resources,
        ).record

    def get_listing_document(
        self,
        listing_id: int,
        *,
        include_resources: bool = True,
    ) -> HostawayObjectDocument:
        """Fetch a listing while retaining its actual response envelope schema."""
        if listing_id <= 0:
            raise ValueError("listing_id must be positive.")
        payload = self._get_json(
            f"/listings/{listing_id}",
            params=[("includeResources", int(include_resources))],
        )
        return inspect_object_response(payload)

    def get_amenities(self) -> list[dict[str, Any]]:
        """Fetch the documented global Hostaway amenity definitions."""
        return list(self.get_amenities_page().records)

    def get_amenities_page(self) -> HostawayCollectionPage:
        """Fetch amenity definitions with their standard response metadata."""
        payload = self._get_json("/amenities", params=[])
        return inspect_collection_response(payload)

    def get_listing_calendar(
        self,
        listing_id: int,
        *,
        start_date: date,
        end_date: date,
        include_resources: bool = False,
    ) -> CalendarDocument:
        """Fetch and validate a bounded calendar window without reservation resources."""
        self._validate_stay_range(
            listing_id=listing_id,
            start_date=start_date,
            end_date=end_date,
        )
        payload = self._get_json(
            f"/listings/{listing_id}/calendar",
            params=[
                ("startDate", start_date.isoformat()),
                ("endDate", end_date.isoformat()),
                ("includeResources", int(include_resources)),
            ],
        )
        return validate_calendar_response(payload)

    def calculate_price(
        self,
        listing_id: int,
        *,
        check_in: date,
        check_out: date,
        guests: int,
        fallback_currency: str = "",
    ) -> PriceQuote:
        """Calculate a price with priceDetails v2; this never creates a reservation."""
        self._validate_stay_range(
            listing_id=listing_id,
            start_date=check_in,
            end_date=check_out,
        )
        if isinstance(guests, bool) or not isinstance(guests, int) or guests <= 0:
            raise ValueError("guests must be a positive integer.")
        payload = self._post_json(
            f"/listings/{listing_id}/calendar/priceDetails",
            json_body={
                "startingDate": check_in.isoformat(),
                "endingDate": check_out.isoformat(),
                "numberOfGuests": guests,
                "version": 2,
            },
        )
        return validate_price_response(
            payload,
            listing_id=listing_id,
            check_in=check_in,
            check_out=check_out,
            guests=guests,
            fallback_currency=fallback_currency,
        )

    def _get_json(
        self,
        path: str,
        *,
        params: list[tuple[str, str | int]],
    ) -> Any:
        response = self._get_with_retry(path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise HostawayResponseError("Hostaway returned invalid JSON.") from exc

    def _post_json(self, path: str, *, json_body: dict[str, Any]) -> Any:
        response = self._authenticated_post(path, json_body=json_body)
        self._raise_for_response(response, retry_exhausted=False)
        try:
            return response.json()
        except ValueError as exc:
            raise HostawayResponseError("Hostaway returned invalid JSON.") from exc

    def _get_with_retry(
        self,
        path: str,
        *,
        params: list[tuple[str, str | int]],
    ) -> httpx.Response:
        for attempt in range(1, self.max_get_attempts + 1):
            response = self._authenticated_get(path, params=params)

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_get_attempts:
                    delay = self._retry_delay(response, attempt)
                    logger.warning(
                        "Hostaway GET retry scheduled: status=%s attempt=%s",
                        response.status_code,
                        attempt,
                    )
                    self.sleeper(delay)
                    continue
                if response.status_code == 429:
                    raise HostawayRateLimitError(
                        "Hostaway rate limit persisted after bounded retries."
                    )
                raise HostawayServerError("Hostaway server error persisted after bounded retries.")
            self._raise_for_response(response, retry_exhausted=True)
            return response

        raise HostawayNetworkError("Hostaway GET request did not complete.")

    def _authenticated_get(
        self,
        path: str,
        *,
        params: list[tuple[str, str | int]],
    ) -> httpx.Response:
        token = self._token_provider.get_token()
        response = self._request_get(path, params=params, token=token)
        if response.status_code != 403:
            return response

        self._token_provider.invalidate()
        try:
            refreshed_token = self._token_provider.get_token(force_refresh=True)
        except HostawayConfigurationError as exc:
            raise HostawayAuthenticationError(
                "Hostaway returned HTTP 403 and token refresh is not configured."
            ) from exc
        return self._request_get(path, params=params, token=refreshed_token)

    def _authenticated_post(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
    ) -> httpx.Response:
        token = self._token_provider.get_token()
        response = self._request_post(path, json_body=json_body, token=token)
        if response.status_code != 403:
            return response

        self._token_provider.invalidate()
        try:
            refreshed_token = self._token_provider.get_token(force_refresh=True)
        except HostawayConfigurationError as exc:
            raise HostawayAuthenticationError(
                "Hostaway returned HTTP 403 and token refresh is not configured."
            ) from exc
        return self._request_post(path, json_body=json_body, token=refreshed_token)

    def _request_get(
        self,
        path: str,
        *,
        params: list[tuple[str, str | int]],
        token: str,
    ) -> httpx.Response:
        try:
            return self._client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            raise HostawayTimeoutError("Hostaway request timed out.") from exc
        except httpx.NetworkError as exc:
            raise HostawayNetworkError("Hostaway network request failed.") from exc

    def _request_post(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        token: str,
    ) -> httpx.Response:
        try:
            return self._client.post(
                path,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException as exc:
            raise HostawayTimeoutError("Hostaway request timed out.") from exc
        except httpx.NetworkError as exc:
            raise HostawayNetworkError("Hostaway network request failed.") from exc

    @staticmethod
    def _raise_for_response(
        response: httpx.Response,
        *,
        retry_exhausted: bool,
    ) -> None:
        if response.status_code in (401, 403):
            raise HostawayAuthenticationError(
                f"Hostaway rejected credentials with HTTP {response.status_code}."
            )
        if response.status_code == 404:
            raise HostawayNotFoundError("The requested Hostaway resource was not found.")
        if response.status_code == 429:
            suffix = " after bounded retries" if retry_exhausted else ""
            raise HostawayRateLimitError(f"Hostaway rate limit persisted{suffix}.")
        if response.status_code >= 500:
            suffix = " after bounded retries" if retry_exhausted else ""
            raise HostawayServerError(f"Hostaway server error persisted{suffix}.")
        if response.status_code in (400, 409, 422):
            raise HostawayAvailabilityError(
                "Hostaway could not calculate this stay under the current restrictions."
            )
        if response.is_error:
            raise HostawayResponseError(
                f"Hostaway returned unexpected HTTP {response.status_code}."
            )

    @staticmethod
    def _validate_stay_range(
        *,
        listing_id: int,
        start_date: date,
        end_date: date,
    ) -> None:
        if isinstance(listing_id, bool) or not isinstance(listing_id, int) or listing_id <= 0:
            raise ValueError("listing_id must be positive.")
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise ValueError("start_date and end_date must be date values.")
        days = (end_date - start_date).days
        if days < 1:
            raise ValueError("end_date must be after start_date.")
        if days > 366:
            raise ValueError("Hostaway calendar windows cannot exceed 366 days.")

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(30.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(4.0, 0.5 * (2 ** (attempt - 1)))
