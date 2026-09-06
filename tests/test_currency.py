import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from django.conf import settings
from django.template import Context, Template
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.payments.currency import (
    FRESH_CACHE_KEY,
    LKG_CACHE_KEY,
    REFRESH_LOCK_KEY,
    CurrencyError,
    CurrencyService,
    ExchangeRateProvider,
    ExchangeRateResponseError,
    ExchangeRateSnapshot,
    ExchangeRateUnavailableError,
    UnsupportedCurrencyError,
    normalize_amount,
    validate_payment_snapshot,
)


def provider_document(**rate_overrides: object) -> dict[str, object]:
    rates: dict[str, object] = {
        "SAR": 1,
        "MAD": "2.5",
        "USD": "0.25",
        "EUR": "0.20",
    }
    rates.update(rate_overrides)
    return {
        "result": "success",
        "base_code": "SAR",
        "time_last_update_unix": 1_788_480_000,
        "time_next_update_unix": 1_788_566_400,
        "rates": rates,
    }


def fixed_rates() -> ExchangeRateSnapshot:
    return ExchangeRateProvider.validate(provider_document())


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.expires_at: dict[str, float] = {}
        self.writes: list[tuple[str, int | None]] = []
        self.lock = threading.Lock()

    def get(self, key: str, default: object = None) -> object:
        with self.lock:
            expires_at = self.expires_at.get(key)
            if expires_at is not None and expires_at <= time.monotonic():
                self.values.pop(key, None)
                self.expires_at.pop(key, None)
            return self.values.get(key, default)

    def set(self, key: str, value: object, timeout: int | None = None) -> None:
        with self.lock:
            self.values[key] = value
            if timeout is None:
                self.expires_at.pop(key, None)
            else:
                self.expires_at[key] = time.monotonic() + timeout
            self.writes.append((key, timeout))

    def add(self, key: str, value: object, timeout: int | None = None) -> bool:
        with self.lock:
            expires_at = self.expires_at.get(key)
            if expires_at is not None and expires_at <= time.monotonic():
                self.values.pop(key, None)
                self.expires_at.pop(key, None)
            if key in self.values:
                return False
            self.values[key] = value
            if timeout is not None:
                self.expires_at[key] = time.monotonic() + timeout
            return True

    def delete(self, key: str) -> bool:
        with self.lock:
            existed = key in self.values
            self.values.pop(key, None)
            self.expires_at.pop(key, None)
            return existed


class ProviderStub:
    def __init__(
        self,
        snapshot: ExchangeRateSnapshot | None = None,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot or fixed_rates()
        self.error = error
        self.calls = 0

    def fetch(self) -> ExchangeRateSnapshot:
        self.calls += 1
        if self.error:
            raise self.error
        return self.snapshot

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("400", "SAR", Decimal("400.00")),
        ("1000", "MAD", Decimal("400.00")),
        ("100", "USD", Decimal("400.00")),
        ("80", "EUR", Decimal("400.00")),
    ],
)
def test_source_currency_to_sar(amount: str, currency: str, expected: Decimal) -> None:
    assert CurrencyService.source_to_sar(amount, currency, fixed_rates()) == expected


def test_sar_source_amount_is_not_double_converted() -> None:
    assert CurrencyService.source_to_sar("1400", "SAR", fixed_rates()) == Decimal("1400.00")


def test_mad_source_amount_is_divided_by_the_sar_base_rate() -> None:
    assert CurrencyService.source_to_sar("1400", "MAD", fixed_rates()) == Decimal("560.00")


@pytest.mark.parametrize(
    ("currency", "expected"),
    [
        ("SAR", Decimal("400.00")),
        ("MAD", Decimal("1000.00")),
        ("USD", Decimal("100.00")),
        ("EUR", Decimal("80.00")),
    ],
)
def test_sar_to_display_currency(currency: str, expected: Decimal) -> None:
    assert CurrencyService.sar_to_display("400", currency, fixed_rates()) == expected


def test_decimal_rounding_is_centralized_and_half_up() -> None:
    assert normalize_amount(Decimal("1.005"), "SAR") == Decimal("1.01")
    custom = ExchangeRateSnapshot(
        provider="test",
        base_currency="SAR",
        rates={"SAR": Decimal("1"), "MAD": Decimal("3")},
        rate_timestamp=datetime(2026, 9, 4, tzinfo=UTC),
        fetched_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    assert CurrencyService.source_to_sar(Decimal("1"), "MAD", custom) == Decimal("0.33")


def test_invalid_money_and_currency_fail_safely() -> None:
    with pytest.raises(UnsupportedCurrencyError):
        CurrencyService.source_to_sar("10", "GBP", fixed_rates())
    with pytest.raises(CurrencyError):
        normalize_amount(Decimal("NaN"), "SAR")
    with pytest.raises(CurrencyError):
        normalize_amount(Decimal("-0.01"), "SAR")


@pytest.mark.parametrize(
    "document",
    [
        {"result": "error", "base_code": "SAR", "rates": {}},
        {"result": "success", "base_code": "USD", "rates": {}},
        provider_document(MAD=None),
        provider_document(MAD=0),
        provider_document(MAD=-1),
        provider_document(MAD=True),
        provider_document() | {"time_last_update_unix": 0},
    ],
)
def test_provider_rejects_malformed_or_unsafe_data(document: object) -> None:
    with pytest.raises(ExchangeRateResponseError):
        ExchangeRateProvider.validate(document)


def test_provider_rejects_invalid_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ExchangeRateResponseError, match="invalid_json"):
            ExchangeRateProvider(http=http).fetch()


def test_provider_timeout_is_structured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ExchangeRateUnavailableError):
            ExchangeRateProvider(http=http).fetch()


def test_provider_http_500_is_structured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"result": "error"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ExchangeRateUnavailableError, match="http_error"):
            ExchangeRateProvider(http=http).fetch()


def test_fresh_cache_hit_avoids_provider_call() -> None:
    cache = MemoryCache()
    cache.values[FRESH_CACHE_KEY] = fixed_rates().to_cache_value()
    provider = ProviderStub(error=ExchangeRateUnavailableError("must_not_call"))
    service = CurrencyService(provider=provider, cache_backend=cache)
    assert service.get_rates().rate("MAD") == Decimal("2.5")
    assert provider.calls == 0


def test_cache_miss_fetches_once_and_updates_fresh_and_lkg() -> None:
    cache = MemoryCache()
    provider = ProviderStub()
    service = CurrencyService(provider=provider, cache_backend=cache)
    first = service.get_rates()
    second = service.get_rates()
    assert first == second
    assert provider.calls == 1
    assert {key for key, _ in cache.writes} == {FRESH_CACHE_KEY, LKG_CACHE_KEY}


def test_provider_failure_uses_last_known_good() -> None:
    cache = MemoryCache()
    cache.values[LKG_CACHE_KEY] = fixed_rates().to_cache_value()
    provider = ProviderStub(error=ExchangeRateUnavailableError("timeout"))
    service = CurrencyService(provider=provider, cache_backend=cache)
    assert service.get_rates().rate("USD") == Decimal("0.25")
    assert provider.calls == 1


def test_provider_failure_rejects_last_known_good_older_than_max_age() -> None:
    cache = MemoryCache()
    stale = fixed_rates()
    stale = ExchangeRateSnapshot(
        provider=stale.provider,
        base_currency=stale.base_currency,
        rates=stale.rates,
        rate_timestamp=stale.rate_timestamp,
        fetched_at=timezone.now() - timedelta(seconds=settings.FX_LKG_MAX_AGE_SECONDS + 1),
    )
    cache.values[LKG_CACHE_KEY] = stale.to_cache_value()
    service = CurrencyService(
        provider=ProviderStub(error=ExchangeRateUnavailableError("timeout")),
        cache_backend=cache,
    )
    with pytest.raises(ExchangeRateUnavailableError, match="fx_rates_unavailable"):
        service.get_rates()


def test_sar_identity_quote_does_not_need_fresh_or_lkg_rates() -> None:
    cache = MemoryCache()
    cache.values[LKG_CACHE_KEY] = "expired-or-corrupt"
    provider = ProviderStub(error=ExchangeRateUnavailableError("offline"))
    service = CurrencyService(provider=provider, cache_backend=cache)
    created_at = timezone.now()
    quote = service.create_quote(
        source_amount="1400",
        source_currency="SAR",
        display_currency="SAR",
        quote_created_at=created_at,
        quote_expires_at=created_at + timedelta(minutes=10),
    )
    assert quote.payment_amount_sar == Decimal("1400.00")
    assert provider.calls == 0


def test_concurrent_cold_cache_requests_fetch_provider_once() -> None:
    cache = MemoryCache()
    snapshot = fixed_rates()

    class SlowProvider(ProviderStub):
        def fetch(self) -> ExchangeRateSnapshot:
            time.sleep(0.1)
            return super().fetch()

    provider = SlowProvider(snapshot=snapshot)

    def fetch_rates(_index: int) -> ExchangeRateSnapshot:
        return CurrencyService(provider=provider, cache_backend=cache).get_rates()

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(fetch_rates, range(10)))

    assert provider.calls == 1
    assert all(result == snapshot for result in results)
    assert REFRESH_LOCK_KEY not in cache.values


def test_expired_refresh_lock_is_recoverable() -> None:
    cache = MemoryCache()
    cache.values[REFRESH_LOCK_KEY] = "abandoned"
    cache.expires_at[REFRESH_LOCK_KEY] = time.monotonic() + 0.05
    provider = ProviderStub()
    rates = CurrencyService(provider=provider, cache_backend=cache).get_rates()
    assert rates.rate("USD") == Decimal("0.25")
    assert provider.calls == 1
    assert REFRESH_LOCK_KEY not in cache.values


def test_provider_failure_without_lkg_never_fabricates_rate() -> None:
    service = CurrencyService(
        provider=ProviderStub(error=ExchangeRateUnavailableError("timeout")),
        cache_backend=MemoryCache(),
    )
    with pytest.raises(ExchangeRateUnavailableError, match="fx_rates_unavailable"):
        service.get_rates()


def test_quote_snapshot_reproduces_payment_and_display_amounts() -> None:
    service = CurrencyService(provider=ProviderStub(), cache_backend=MemoryCache())
    quote = service.create_quote(
        source_amount=Decimal("1000.0000"),
        source_currency="MAD",
        display_currency="USD",
        quote_created_at=datetime(2026, 9, 4, tzinfo=UTC),
        quote_expires_at=datetime(2026, 9, 4, 0, 15, tzinfo=UTC),
    )
    assert quote.payment_amount_sar == Decimal("400.00")
    assert quote.display_amount == Decimal("100.00")
    validate_payment_snapshot(
        source_amount=Decimal("1000.0000"),
        source_currency="MAD",
        payment_amount_sar=quote.payment_amount_sar,
        snapshot=quote.snapshot,
    )


def test_tampered_snapshot_is_rejected() -> None:
    service = CurrencyService(provider=ProviderStub(), cache_backend=MemoryCache())
    quote = service.create_quote(
        source_amount="1000",
        source_currency="MAD",
        display_currency="USD",
        quote_created_at=datetime(2026, 9, 4, tzinfo=UTC),
        quote_expires_at=datetime(2026, 9, 4, 0, 15, tzinfo=UTC),
    )
    tampered = dict(quote.snapshot)
    tampered["payment_amount_sar"] = "1.00"
    with pytest.raises(ExchangeRateResponseError):
        validate_payment_snapshot(
            source_amount="1000",
            source_currency="MAD",
            payment_amount_sar="400",
            snapshot=tampered,
        )


def test_one_rate_fetch_supports_multiple_money_values_on_one_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def get_rates(_service: CurrencyService) -> ExchangeRateSnapshot:
        nonlocal calls
        calls += 1
        return fixed_rates()

    monkeypatch.setattr(CurrencyService, "get_rates", get_rates)
    request = RequestFactory().get("/properties/")
    output = Template(
        "{% load currency %}{% display_money first 'SAR' %}|{% display_money second 'MAD' %}"
    ).render(
        Context(
            {
                "request": request,
                "display_currency": "USD",
                "first": Decimal("400"),
                "second": Decimal("1000"),
            }
        )
    )
    assert calls == 1
    assert output.count("USD") == 2
    assert "<script" not in output


@pytest.mark.django_db
def test_display_currency_preference_is_validated_and_persisted() -> None:
    client = Client()
    response = client.post(
        reverse("payments:set_currency"),
        {"currency": "usd", "next": "/properties/"},
    )
    assert response.status_code == 302
    assert response.url == "/properties/"
    assert client.session["display_currency"] == "USD"
    assert response.cookies["lsa_display_currency"].value == "USD"

    rejected = client.post(
        reverse("payments:set_currency"),
        {"currency": "<script>", "next": "https://attacker.example/"},
    )
    assert rejected.status_code == 400
    assert client.session["display_currency"] == "USD"
