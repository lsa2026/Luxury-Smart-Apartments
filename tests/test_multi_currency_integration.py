from decimal import Decimal
from functools import partial

import pytest
from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from django.urls import reverse

from apps.payments.currency import DISPLAY_CURRENCY_SESSION_KEY
from apps.payments.hyperpay.service import HyperPayService
from apps.payments.models import PaymentAttempt
from apps.payments.views import HyperPayBookingCheckoutView
from apps.properties.models import Property
from apps.reservations.models import BookingIntent, BookingQuote
from apps.reservations.security import SESSION_MARKER_KEY, hash_session_marker
from apps.reservations.services.booking import (
    consume_revalidated_quote,
    create_quote_for_property,
)
from tests.test_booking_models_services import guest_data, make_availability, make_property
from tests.test_currency import MemoryCache, ProviderStub
from tests.test_hyperpay import HYPERPAY_SETTINGS, HyperPayStub

pytestmark = pytest.mark.django_db


def create_flow(
    *,
    source_amount: Decimal,
    source_currency: str,
    display_currency: str,
    local_currency: str | None = None,
    session_hash: str = "a" * 64,
) -> tuple[Property, object, BookingIntent, ProviderStub]:
    property_obj = make_property()
    if local_currency:
        property_obj.currency_code = local_currency
        property_obj.city = "Marrakesh"
        property_obj.save(update_fields=["currency_code", "city"])
    availability = make_availability(
        property_obj,
        total=source_amount,
        currency=source_currency,
    )
    provider = ProviderStub()
    from apps.payments.currency import CurrencyService

    currency_service = CurrencyService(provider=provider, cache_backend=MemoryCache())
    quote = create_quote_for_property(
        availability,
        property_obj=property_obj,
        session_hash=session_hash,
        selected_display_currency=display_currency,
        currency_service=currency_service,
    )
    outcome = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=session_hash,
        idempotency_key=f"multi-currency-{BookingIntent.objects.count():032d}",
        guest_data=guest_data(),
        revalidated=availability,
        selected_display_currency=display_currency,
    )
    assert outcome.intent is not None
    return property_obj, quote, outcome.intent, provider


@override_settings(**HYPERPAY_SETTINGS)
def test_riyadh_sar_source_displays_usd_but_hyperpay_receives_sar() -> None:
    _property, quote, intent, _provider = create_flow(
        source_amount=Decimal("1000"),
        source_currency="SAR",
        display_currency="USD",
    )
    assert quote.total_price == Decimal("1000.0000")
    assert quote.currency == "SAR"
    assert quote.payment_amount_sar == Decimal("1000.00")
    assert quote.exchange_rate_snapshot["display_amount"] == "250.00"

    gateway = HyperPayStub()
    checkout = HyperPayService(client=gateway).create_checkout(intent)
    assert checkout.attempt.amount == Decimal("1000.00")
    assert checkout.attempt.currency == "SAR"
    assert gateway.payload["amount"] == "1000.00"
    assert gateway.payload["currency"] == "SAR"


@override_settings(**HYPERPAY_SETTINGS)
def test_marrakesh_mad_source_converts_once_and_hyperpay_receives_400_sar() -> None:
    _property, quote, intent, provider = create_flow(
        source_amount=Decimal("1000"),
        source_currency="MAD",
        display_currency="USD",
        local_currency="MAD",
    )
    assert quote.total_price == Decimal("1000.0000")
    assert quote.currency == "MAD"
    assert quote.payment_amount_sar == Decimal("400.00")
    assert quote.exchange_rate_snapshot["display_amount"] == "100.00"
    assert provider.calls == 1

    gateway = HyperPayStub()
    checkout = HyperPayService(client=gateway).create_checkout(intent)
    assert checkout.attempt.amount == Decimal("400.00")
    assert checkout.attempt.currency == "SAR"
    assert gateway.payload["amount"] == "400.00"
    assert gateway.payload["currency"] == "SAR"


@override_settings(**HYPERPAY_SETTINGS)
def test_marrakesh_property_returning_sar_is_never_treated_as_mad() -> None:
    _property, quote, intent, _provider = create_flow(
        source_amount=Decimal("400"),
        source_currency="SAR",
        display_currency="SAR",
        local_currency="MAD",
    )
    assert quote.currency == "SAR"
    assert quote.payment_amount_sar == Decimal("400.00")
    checkout = HyperPayService(client=HyperPayStub()).create_checkout(intent)
    assert checkout.attempt.amount == Decimal("400.00")


def test_changing_display_currency_keeps_locked_payment_amount() -> None:
    property_obj = make_property()
    availability = make_availability(
        property_obj,
        total=Decimal("1000"),
        currency="MAD",
    )
    from apps.payments.currency import CurrencyService

    service = CurrencyService(provider=ProviderStub(), cache_backend=MemoryCache())
    quote = create_quote_for_property(
        availability,
        property_obj=property_obj,
        session_hash="a" * 64,
        selected_display_currency="USD",
        currency_service=service,
    )
    outcome = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="display-change-000000000000000000",
        guest_data=guest_data(),
        revalidated=availability,
        selected_display_currency="EUR",
    )
    assert outcome.intent is not None
    assert quote.payment_amount_sar == Decimal("400.00")
    assert outcome.intent.payment_amount_sar == Decimal("400.00")
    assert outcome.intent.selected_display_currency == "EUR"
    assert outcome.intent.exchange_rate_snapshot["display_amount"] == "80.00"


def test_unknown_hostaway_currency_fails_without_guessing() -> None:
    property_obj = make_property()
    availability = make_availability(
        property_obj,
        total=Decimal("100"),
        currency="GBP",
    )
    from apps.payments.currency import CurrencyService

    with pytest.raises(ValueError, match="currency_conversion_unavailable"):
        create_quote_for_property(
            availability,
            property_obj=property_obj,
            session_hash="a" * 64,
            selected_display_currency="SAR",
            currency_service=CurrencyService(
                provider=ProviderStub(),
                cache_backend=MemoryCache(),
            ),
        )


@override_settings(**HYPERPAY_SETTINGS)
def test_browser_amount_and_currency_tampering_cannot_change_hyperpay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "multi-currency-owner"
    session_hash = hash_session_marker(marker)
    _property, _quote, intent, _provider = create_flow(
        source_amount=Decimal("1000"),
        source_currency="MAD",
        display_currency="USD",
        local_currency="MAD",
        session_hash=session_hash,
    )
    browser = Client()
    session = browser.session
    session[SESSION_MARKER_KEY] = marker
    session[DISPLAY_CURRENCY_SESSION_KEY] = "USD"
    session.save()
    gateway = HyperPayStub()
    monkeypatch.setattr(
        HyperPayBookingCheckoutView,
        "service_class",
        staticmethod(partial(HyperPayService, client=gateway)),
    )
    response = browser.post(
        reverse("payments:hyperpay_booking", args=[intent.public_reference]),
        {"amount": "1", "currency": "USD"},
    )
    assert response.status_code == 200
    content = response.content.decode()
    visible_total = content.split("checkout__total--display", 1)[1].split("</div>", 1)[0]
    assert "USD" in visible_total
    assert "SAR" not in visible_total
    assert "checkout__total--payment" not in content
    attempt = PaymentAttempt.objects.get()
    assert attempt.amount == Decimal("400.00")
    assert attempt.currency == "SAR"
    assert gateway.payload["amount"] == "400.00"
    assert gateway.payload["currency"] == "SAR"


def test_database_constraints_reject_negative_sar_and_non_sar_hyperpay() -> None:
    _property, quote, intent, _provider = create_flow(
        source_amount=Decimal("1000"),
        source_currency="SAR",
        display_currency="USD",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        BookingQuote.objects.filter(pk=quote.pk).update(payment_amount_sar=Decimal("-0.01"))
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentAttempt.objects.create(
            booking_intent=intent,
            provider="hyperpay",
            amount=Decimal("1000"),
            currency="MAD",
            idempotency_key="db-constraint-non-sar-hyperpay-0000000001",
        )
