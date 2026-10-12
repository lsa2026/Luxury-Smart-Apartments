from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.test import Client, RequestFactory, override_settings

from apps.core.marketing import payment_analytics_fields, prepare_purchase_event
from apps.payments.models import PaymentAttempt
from apps.payments.views import _render_hyperpay_checkout
from tests.test_booking_modifications_phase6 import confirmed_reservation
from tests.test_hyperpay import HYPERPAY_SETTINGS, payable_intent


@pytest.mark.parametrize(
    ("raw", "kind", "brand"),
    [
        ("MADA", "card", "mada"),
        ("VISA", "card", "visa"),
        ("MASTER", "card", "mastercard"),
        ("APPLEPAY", "apple_pay", "apple_pay"),
        ("", "unknown", "unknown"),
        ("private-token", "unknown", "unknown"),
    ],
)
def test_gateway_brand_is_allowlisted_without_inferred_wallet_network(raw, kind, brand):
    assert payment_analytics_fields(SimpleNamespace(payment_brand=raw)) == {
        "payment_type": kind,
        "payment_brand": brand,
    }


@pytest.mark.django_db
@pytest.mark.parametrize("brand", ["MADA", "VISA", "MASTER", "APPLEPAY", ""])
def test_confirmed_purchase_keeps_brand_amount_property_and_existing_receipt(brand):
    reservation = confirmed_reservation()
    attempt = SimpleNamespace(status=PaymentAttempt.Status.SUCCEEDED, payment_brand=brand)
    event, receipt = prepare_purchase_event(payment_attempt=attempt, reservation=reservation)
    assert event["payment_brand"] == payment_analytics_fields(attempt)["payment_brand"]
    assert event["value"] == float(reservation.total_price)
    assert event["property_id"] == reservation.property.slug
    assert event["currency"] == reservation.currency
    again, same = prepare_purchase_event(payment_attempt=attempt, reservation=reservation)
    assert same.pk == receipt.pk
    assert again["transaction_id"] == event["transaction_id"]


@pytest.mark.django_db
@override_settings(**HYPERPAY_SETTINGS)
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_secure_payment_exposes_only_public_context(language):
    intent = payable_intent()
    attempt = PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="hyperpay",
        amount=Decimal("1100"),
        currency="SAR",
        idempotency_key=f"journey-{language}",
        provider_checkout_id=f"journey-checkout-{language}",
        widget_integrity="sha384-YWJj",
    )
    request = RequestFactory().get("/payments/hyperpay/booking/")
    request.session = Client().session
    request.LANGUAGE_CODE = language
    response = _render_hyperpay_checkout(request, attempt=attempt, intent=intent)
    content = response.content.decode()
    assert "data-payment-journey" in content
    assert f'data-analytics-property-id="{intent.property.slug}"' in content
    assert 'data-analytics-value="1100"' in content
    assert "payment-journey.js?v=1" in content
    assert "onAfterSubmit:" in content
    assert "onError:" in content
    assert "onBeforeSubmitCard:" not in content
    assert intent.guest_email not in content
    assert response["Cache-Control"] == "no-store, private"


def test_journey_does_not_read_pci_fields_or_change_payment_flow():
    script = Path("static/js/payment-journey.js").read_text()
    for forbidden in (
        "card.number",
        "card.cvv",
        "card.holder",
        "serialize",
        "preventDefault",
        "localStorage",
        "sessionStorage",
        "fetch(",
        "error.message",
    ):
        assert forbidden not in script
