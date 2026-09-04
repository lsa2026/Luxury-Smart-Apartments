"""Server-side exchange rates and immutable checkout currency snapshots."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from django.conf import settings
from django.core.cache import cache as default_cache
from django.utils import timezone

logger = logging.getLogger(__name__)

PAYMENT_CURRENCY = "SAR"
FRESH_CACHE_KEY = "fx:rates:sar:v1:fresh"
LKG_CACHE_KEY = "fx:rates:sar:v1:lkg"
REFRESH_LOCK_KEY = "fx:rates:sar:v1:refresh-lock"
SNAPSHOT_VERSION = 1
DISPLAY_CURRENCY_SESSION_KEY = "display_currency"
REFRESH_POLL_SECONDS = 0.05


class CurrencyError(Exception):
    """Base class for safe, user-facing currency failures."""


class UnsupportedCurrencyError(CurrencyError):
    pass


class ExchangeRateUnavailableError(CurrencyError):
    pass


class ExchangeRateResponseError(CurrencyError):
    pass


class CacheBackend(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any, timeout: int | None = None) -> None: ...

    def add(self, key: str, value: Any, timeout: int | None = None) -> bool: ...

    def delete(self, key: str) -> bool: ...


def normalize_currency(value: object) -> str:
    code = str(value or "").strip().upper()
    supported = tuple(settings.FX_SUPPORTED_CURRENCIES)
    if code not in supported:
        raise UnsupportedCurrencyError("unsupported_currency")
    return code


def selected_currency(request: object) -> str:
    """Read an untrusted display-only preference, falling back safely to SAR."""

    session = getattr(request, "session", {})
    raw_value = session.get(DISPLAY_CURRENCY_SESSION_KEY, "")
    if not raw_value:
        cookies = getattr(request, "COOKIES", {})
        raw_value = cookies.get(settings.FX_PREFERENCE_COOKIE, "")
    try:
        return normalize_currency(raw_value or PAYMENT_CURRENCY)
    except UnsupportedCurrencyError:
        return PAYMENT_CURRENCY


def currency_quantum(currency: object) -> Decimal:
    code = normalize_currency(currency)
    minor_units = settings.FX_CURRENCY_MINOR_UNITS.get(code)
    if not isinstance(minor_units, int) or minor_units < 0:
        raise UnsupportedCurrencyError("currency_minor_units_unavailable")
    return Decimal(1).scaleb(-minor_units)


def normalize_amount(value: object, currency: object) -> Decimal:
    """Validate and apply the one centralized monetary rounding policy."""

    amount = _validated_amount(value)
    return amount.quantize(currency_quantum(currency), rounding=ROUND_HALF_UP)


def _validated_amount(value: object) -> Decimal:
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CurrencyError("invalid_amount") from exc
    if not amount.is_finite() or amount < 0:
        raise CurrencyError("invalid_amount")
    return amount


def _decimal_rate(value: object, code: str) -> Decimal:
    if isinstance(value, bool):
        raise ExchangeRateResponseError(f"invalid_rate_{code}")
    try:
        rate = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeRateResponseError(f"invalid_rate_{code}") from exc
    if not rate.is_finite() or rate <= 0:
        raise ExchangeRateResponseError(f"invalid_rate_{code}")
    return rate


def _aware_datetime(value: object, field: str) -> datetime:
    if isinstance(value, bool):
        raise ExchangeRateResponseError(f"invalid_{field}")
    try:
        timestamp = int(value)
    except (TypeError, ValueError) as exc:
        raise ExchangeRateResponseError(f"invalid_{field}") from exc
    if timestamp <= 0:
        raise ExchangeRateResponseError(f"invalid_{field}")
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ExchangeRateResponseError(f"invalid_{field}") from exc


@dataclass(frozen=True, slots=True)
class ExchangeRateSnapshot:
    provider: str
    base_currency: str
    rates: Mapping[str, Decimal]
    rate_timestamp: datetime
    fetched_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "rates", MappingProxyType(dict(self.rates)))

    def rate(self, currency: object) -> Decimal:
        code = normalize_currency(currency)
        try:
            return self.rates[code]
        except KeyError as exc:
            raise ExchangeRateUnavailableError(f"missing_rate_{code}") from exc

    def to_cache_value(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "base_currency": self.base_currency,
            "rates": {code: format(rate, "f") for code, rate in self.rates.items()},
            "rate_timestamp": self.rate_timestamp.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
        }

    @classmethod
    def from_cache_value(cls, value: object) -> ExchangeRateSnapshot:
        if not isinstance(value, dict):
            raise ExchangeRateResponseError("invalid_cached_rates")
        provider = value.get("provider")
        base_currency = value.get("base_currency")
        raw_rates = value.get("rates")
        if not isinstance(provider, str) or not provider.strip():
            raise ExchangeRateResponseError("invalid_cached_provider")
        if base_currency != PAYMENT_CURRENCY or not isinstance(raw_rates, dict):
            raise ExchangeRateResponseError("invalid_cached_base")
        rates = {
            code: _decimal_rate(raw_rates.get(code), code)
            for code in settings.FX_SUPPORTED_CURRENCIES
        }
        if rates[PAYMENT_CURRENCY] != Decimal("1"):
            raise ExchangeRateResponseError("invalid_sar_rate")
        try:
            rate_timestamp = datetime.fromisoformat(str(value.get("rate_timestamp")))
            fetched_at = datetime.fromisoformat(str(value.get("fetched_at")))
        except ValueError as exc:
            raise ExchangeRateResponseError("invalid_cached_timestamp") from exc
        if timezone.is_naive(rate_timestamp) or timezone.is_naive(fetched_at):
            raise ExchangeRateResponseError("invalid_cached_timestamp")
        return cls(provider.strip(), base_currency, rates, rate_timestamp, fetched_at)


class ExchangeRateProvider:
    """Strict client for the configured SAR-base exchange-rate provider."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        parsed = urlparse(settings.FX_PROVIDER_URL)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ExchangeRateResponseError("fx_provider_url_invalid")
        self._owns_http = http is None
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(
                settings.FX_REQUEST_READ_TIMEOUT,
                connect=settings.FX_REQUEST_CONNECT_TIMEOUT,
            ),
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def fetch(self) -> ExchangeRateSnapshot:
        try:
            response = self.http.get(settings.FX_PROVIDER_URL)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ExchangeRateUnavailableError("fx_provider_unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise ExchangeRateUnavailableError("fx_provider_http_error")
        try:
            document = response.json()
        except (TypeError, ValueError) as exc:
            raise ExchangeRateResponseError("fx_provider_invalid_json") from exc
        return self.validate(document)

    @staticmethod
    def validate(document: object) -> ExchangeRateSnapshot:
        if not isinstance(document, dict):
            raise ExchangeRateResponseError("fx_provider_invalid_document")
        if document.get("result") != "success":
            raise ExchangeRateResponseError("fx_provider_unsuccessful")
        if document.get("base_code") != PAYMENT_CURRENCY:
            raise ExchangeRateResponseError("fx_provider_invalid_base")
        raw_rates = document.get("rates")
        if not isinstance(raw_rates, dict):
            raise ExchangeRateResponseError("fx_provider_missing_rates")
        rates = {
            code: _decimal_rate(raw_rates.get(code), code)
            for code in settings.FX_SUPPORTED_CURRENCIES
        }
        if rates[PAYMENT_CURRENCY] != Decimal("1"):
            raise ExchangeRateResponseError("fx_provider_invalid_sar_rate")
        now = timezone.now()
        raw_timestamp = document.get("time_last_update_unix")
        rate_timestamp = (
            _aware_datetime(raw_timestamp, "time_last_update_unix")
            if raw_timestamp is not None
            else now
        )
        raw_next_timestamp = document.get("time_next_update_unix")
        if raw_next_timestamp is not None:
            _aware_datetime(raw_next_timestamp, "time_next_update_unix")
        return ExchangeRateSnapshot(
            provider=settings.FX_PROVIDER_NAME,
            base_currency=PAYMENT_CURRENCY,
            rates=rates,
            rate_timestamp=rate_timestamp,
            fetched_at=now,
        )


@dataclass(frozen=True, slots=True)
class CurrencyQuote:
    source_amount: Decimal
    source_currency: str
    payment_amount_sar: Decimal
    display_amount: Decimal
    display_currency: str
    snapshot: Mapping[str, object]


class CurrencyService:
    """Convert through SAR and create an auditable, immutable quote snapshot."""

    def __init__(
        self,
        *,
        provider: ExchangeRateProvider | None = None,
        cache_backend: CacheBackend | None = None,
    ) -> None:
        self.provider = provider or ExchangeRateProvider()
        self._owns_provider = provider is None
        self.cache = cache_backend or default_cache

    def close(self) -> None:
        if self._owns_provider:
            self.provider.close()

    def __enter__(self) -> CurrencyService:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get_rates(self, *, force_refresh: bool = False) -> ExchangeRateSnapshot:
        if not force_refresh:
            cached = self._read_cache(FRESH_CACHE_KEY)
            if cached is not None:
                logger.info("FX_CACHE_HIT base_currency=SAR")
                return cached
        logger.info("FX_CACHE_MISS base_currency=SAR")
        return self._refresh_with_lock(force_refresh=force_refresh)

    def _refresh_with_lock(self, *, force_refresh: bool) -> ExchangeRateSnapshot:
        deadline = time.monotonic() + settings.FX_REFRESH_LOCK_WAIT_SECONDS
        waited = False
        while True:
            if waited or not force_refresh:
                cached = self._read_cache(FRESH_CACHE_KEY)
                if cached is not None:
                    logger.info("FX_CACHE_HIT_AFTER_WAIT base_currency=SAR")
                    return cached
            if waited and self.cache.get(REFRESH_LOCK_KEY) is None:
                lkg = self._read_lkg()
                if lkg is not None:
                    logger.warning(
                        "FX_LKG_USED_AFTER_REFRESH_FAILURE provider=%s rate_timestamp=%s",
                        lkg.provider,
                        lkg.rate_timestamp.isoformat(),
                    )
                    return lkg

            lock_token = secrets.token_urlsafe(16)
            if self.cache.add(
                REFRESH_LOCK_KEY,
                lock_token,
                timeout=settings.FX_REFRESH_LOCK_TTL_SECONDS,
            ):
                try:
                    return self._fetch_or_lkg()
                finally:
                    self._release_refresh_lock(lock_token)

            waited = True
            if self.cache.get(REFRESH_LOCK_KEY) is None:
                lkg = self._read_lkg()
                if lkg is not None:
                    logger.warning(
                        "FX_LKG_USED_AFTER_REFRESH_FAILURE provider=%s rate_timestamp=%s",
                        lkg.provider,
                        lkg.rate_timestamp.isoformat(),
                    )
                    return lkg
            if time.monotonic() >= deadline:
                lkg = self._read_lkg()
                if lkg is not None:
                    logger.warning(
                        "FX_LKG_USED_AFTER_REFRESH_WAIT provider=%s rate_timestamp=%s",
                        lkg.provider,
                        lkg.rate_timestamp.isoformat(),
                    )
                    return lkg
                raise ExchangeRateUnavailableError("fx_refresh_wait_timeout")
            time.sleep(REFRESH_POLL_SECONDS)

    def _fetch_or_lkg(self) -> ExchangeRateSnapshot:
        try:
            snapshot = self.provider.fetch()
        except CurrencyError as exc:
            logger.warning("FX_PROVIDER_FAILURE code=%s", str(exc))
            lkg = self._read_lkg()
            if lkg is not None:
                logger.warning(
                    "FX_LKG_USED provider=%s rate_timestamp=%s",
                    lkg.provider,
                    lkg.rate_timestamp.isoformat(),
                )
                return lkg
            raise ExchangeRateUnavailableError("fx_rates_unavailable") from exc
        value = snapshot.to_cache_value()
        self.cache.set(FRESH_CACHE_KEY, value, timeout=settings.FX_RATE_CACHE_TTL_SECONDS)
        self.cache.set(LKG_CACHE_KEY, value, timeout=None)
        logger.info(
            "FX_PROVIDER_SUCCESS provider=%s rate_timestamp=%s",
            snapshot.provider,
            snapshot.rate_timestamp.isoformat(),
        )
        return snapshot

    def _read_lkg(self) -> ExchangeRateSnapshot | None:
        snapshot = self._read_cache(LKG_CACHE_KEY)
        if snapshot is None:
            return None
        age = timezone.now() - snapshot.fetched_at
        if age > timedelta(seconds=settings.FX_LKG_MAX_AGE_SECONDS):
            logger.warning(
                "FX_LKG_REJECTED_STALE provider=%s fetched_at=%s max_age_seconds=%s",
                snapshot.provider,
                snapshot.fetched_at.isoformat(),
                settings.FX_LKG_MAX_AGE_SECONDS,
            )
            return None
        return snapshot

    def _release_refresh_lock(self, lock_token: str) -> None:
        try:
            if self.cache.get(REFRESH_LOCK_KEY) == lock_token:
                self.cache.delete(REFRESH_LOCK_KEY)
        except Exception:
            logger.warning("FX_REFRESH_LOCK_RELEASE_FAILURE")

    def _read_cache(self, key: str) -> ExchangeRateSnapshot | None:
        value = self.cache.get(key)
        if value is None:
            return None
        try:
            return ExchangeRateSnapshot.from_cache_value(value)
        except CurrencyError:
            logger.warning("FX_PROVIDER_FAILURE code=invalid_cached_rates cache_key=%s", key)
            return None

    @staticmethod
    def source_to_sar(
        amount: object,
        source_currency: object,
        rates: ExchangeRateSnapshot | Mapping[str, object] | None = None,
    ) -> Decimal:
        source = normalize_currency(source_currency)
        source_amount = _validated_amount(amount)
        if source == PAYMENT_CURRENCY:
            return normalize_amount(source_amount, PAYMENT_CURRENCY)
        snapshot = _coerce_snapshot(rates)
        return normalize_amount(source_amount / snapshot.rate(source), PAYMENT_CURRENCY)

    @staticmethod
    def sar_to_display(
        amount_sar: object,
        display_currency: object,
        rates: ExchangeRateSnapshot | Mapping[str, object] | None = None,
    ) -> Decimal:
        display = normalize_currency(display_currency)
        payment_amount = normalize_amount(amount_sar, PAYMENT_CURRENCY)
        if display == PAYMENT_CURRENCY:
            return payment_amount
        snapshot = _coerce_snapshot(rates)
        return normalize_amount(payment_amount * snapshot.rate(display), display)

    def convert(
        self,
        amount: object,
        source_currency: object,
        display_currency: object,
        rates: ExchangeRateSnapshot | Mapping[str, object] | None = None,
    ) -> Decimal:
        source = normalize_currency(source_currency)
        display = normalize_currency(display_currency)
        if source == display:
            return normalize_amount(amount, source)
        snapshot = _coerce_snapshot(rates) if rates is not None else self.get_rates()
        amount_sar = self.source_to_sar(amount, source, snapshot)
        return self.sar_to_display(amount_sar, display, snapshot)

    def create_quote(
        self,
        *,
        source_amount: object,
        source_currency: object,
        display_currency: object,
        quote_created_at: datetime,
        quote_expires_at: datetime,
    ) -> CurrencyQuote:
        source = normalize_currency(source_currency)
        display = normalize_currency(display_currency)
        source_value = _validated_amount(source_amount)
        if source == PAYMENT_CURRENCY and display == PAYMENT_CURRENCY:
            rates = _identity_snapshot(quote_created_at)
        else:
            rates = self.get_rates()
        payment_amount = self.source_to_sar(source_value, source, rates)
        display_amount = self.sar_to_display(payment_amount, display, rates)
        snapshot: dict[str, object] = {
            "version": SNAPSHOT_VERSION,
            "provider": rates.provider,
            "base_currency": PAYMENT_CURRENCY,
            "rate_timestamp": rates.rate_timestamp.isoformat(),
            "rates": {code: format(rate, "f") for code, rate in rates.rates.items()},
            "source_amount": format(source_value, "f"),
            "source_currency": source,
            "payment_amount_sar": format(payment_amount, "f"),
            "payment_currency": PAYMENT_CURRENCY,
            "selected_display_currency": display,
            "display_amount": format(display_amount, "f"),
            "source_rate_per_sar": format(rates.rate(source), "f"),
            "display_rate_per_sar": format(rates.rate(display), "f"),
            "quote_created_at": quote_created_at.isoformat(),
            "quote_expires_at": quote_expires_at.isoformat(),
        }
        logger.info(
            "FX_CONVERSION_CREATED source_currency=%s payment_currency=SAR "
            "display_currency=%s rate_timestamp=%s",
            source,
            display,
            rates.rate_timestamp.isoformat(),
        )
        return CurrencyQuote(
            source_amount=source_value,
            source_currency=source,
            payment_amount_sar=payment_amount,
            display_amount=display_amount,
            display_currency=display,
            snapshot=MappingProxyType(snapshot),
        )


def rates_from_audit_snapshot(value: object) -> ExchangeRateSnapshot:
    if not isinstance(value, Mapping):
        raise ExchangeRateUnavailableError("fx_snapshot_missing")
    raw_rates = value.get("rates")
    if not isinstance(raw_rates, Mapping):
        raise ExchangeRateUnavailableError("fx_snapshot_rates_missing")
    rates: dict[str, Decimal] = {}
    for code in settings.FX_SUPPORTED_CURRENCIES:
        if code not in raw_rates:
            continue
        rates[code] = _decimal_rate(raw_rates[code], code)
    if rates.get(PAYMENT_CURRENCY) != Decimal("1"):
        raise ExchangeRateResponseError("fx_snapshot_invalid_sar_rate")
    try:
        rate_timestamp = datetime.fromisoformat(str(value.get("rate_timestamp")))
    except ValueError as exc:
        raise ExchangeRateResponseError("fx_snapshot_invalid_timestamp") from exc
    if timezone.is_naive(rate_timestamp):
        raise ExchangeRateResponseError("fx_snapshot_invalid_timestamp")
    return ExchangeRateSnapshot(
        provider=str(value.get("provider") or "snapshot"),
        base_currency=PAYMENT_CURRENCY,
        rates=rates,
        rate_timestamp=rate_timestamp,
        fetched_at=rate_timestamp,
    )


def validate_payment_snapshot(
    *,
    source_amount: object,
    source_currency: object,
    payment_amount_sar: object,
    snapshot: object,
) -> None:
    """Prove persisted audit fields still reproduce the authoritative SAR amount."""

    if not isinstance(snapshot, Mapping):
        raise ExchangeRateResponseError("fx_snapshot_missing")
    source = normalize_currency(source_currency)
    payment = normalize_amount(payment_amount_sar, PAYMENT_CURRENCY)
    if snapshot.get("source_currency") != source:
        raise ExchangeRateResponseError("fx_snapshot_source_currency_mismatch")
    try:
        snap_source_amount = Decimal(str(snapshot.get("source_amount")))
        snap_payment_amount = Decimal(str(snapshot.get("payment_amount_sar")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeRateResponseError("fx_snapshot_amount_invalid") from exc
    if snap_source_amount != _validated_amount(source_amount):
        raise ExchangeRateResponseError("fx_snapshot_source_amount_mismatch")
    if normalize_amount(snap_payment_amount, PAYMENT_CURRENCY) != payment:
        raise ExchangeRateResponseError("fx_snapshot_payment_amount_mismatch")
    if snapshot.get("payment_currency") != PAYMENT_CURRENCY:
        raise ExchangeRateResponseError("fx_snapshot_payment_currency_mismatch")
    rates = rates_from_audit_snapshot(snapshot)
    expected = CurrencyService.source_to_sar(source_amount, source, rates)
    if expected != payment:
        raise ExchangeRateResponseError("fx_snapshot_conversion_mismatch")


def _coerce_snapshot(
    value: ExchangeRateSnapshot | Mapping[str, object] | None,
) -> ExchangeRateSnapshot:
    if isinstance(value, ExchangeRateSnapshot):
        return value
    if isinstance(value, Mapping):
        return rates_from_audit_snapshot(value)
    raise ExchangeRateUnavailableError("fx_rates_required")


def _identity_snapshot(at: datetime) -> ExchangeRateSnapshot:
    return ExchangeRateSnapshot(
        provider="identity",
        base_currency=PAYMENT_CURRENCY,
        rates={PAYMENT_CURRENCY: Decimal("1")},
        rate_timestamp=at,
        fetched_at=at,
    )
