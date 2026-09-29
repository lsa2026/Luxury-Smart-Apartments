"""UAT-only Apple Pay express checkout: contact data arrives after Wallet approval."""

import json
from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.payments.hyperpay.result_codes import HyperPayStatus
from apps.payments.hyperpay.service import VerificationOutcome
from apps.payments.models import PaymentAttempt
from apps.payments.views import ApplePayFastCreateView, HyperPayResultView
from apps.reservations.models import BookingIntent
from apps.reservations.security import SESSION_MARKER_KEY, hash_session_marker
from apps.reservations.signing import quote_reference
from tests.test_booking_models_services import make_availability, make_property, make_quote

pytestmark = pytest.mark.django_db

UAT_SETTINGS = {
    "APPLE_PAY_FAST_CHECKOUT_ENABLED": True,
    "HYPERPAY_ENABLED": True,
    "HYPERPAY_ENVIRONMENT": "test",
    "HYPERPAY_BASE_URL": "https://eu-test.oppwa.com/",
    "HYPERPAY_WIDGET_ORIGIN": "https://eu-test.oppwa.com",
    "HYPERPAY_ENTITY_ID": "synthetic-test-entity",
    "HYPERPAY_PAYMENT_TYPE": "DB",
    "HYPERPAY_ALLOWED_BRANDS": ("MADA", "VISA", "MASTER", "APPLEPAY"),
    "HOSTAWAY_LIVE_BOOKING_ENABLED": False,
}


class CheckoutClientStub:
    payloads = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def create_checkout(self, payload):
        type(self).payloads.append(payload)
        return {
            "id": "ABC123456789.uat01-vm-tx04",
            "integrity": "sha384-YWJj",
            "result": {"code": "000.200.100"},
        }


def owned_quote():
    client = Client()
    session = client.session
    marker = "apple-pay-quote-owner-marker"
    session[SESSION_MARKER_KEY] = marker
    session.save()
    quote = make_quote(
        make_property(), total=Decimal("500.00"),
        session_hash=hash_session_marker(marker),
    )
    return client, quote, quote_reference(quote)


def create_checkout(client, reference):
    return client.post(
        reverse("payments:apple_fast_create", args=[reference]),
        {"terms_accepted": "on", "privacy_accepted": "on"},
    )


def wallet_contact():
    return {
        "shippingContact": {
            "givenName": "Test",
            "familyName": "Guest",
            "emailAddress": "guest@example.invalid",
            "phoneNumber": "+966500000000",
        },
        "billingContact": {
            "addressLines": ["King Fahd Road 10"],
            "locality": "Riyadh",
            "administrativeArea": "Riyadh",
            "countryCode": "SA",
            "postalCode": "12345",
        },
    }


@override_settings(**UAT_SETTINGS)
def test_express_checkout_appears_before_guest_form_and_csp_allows_widget():
    client, quote, reference = owned_quote()
    response = client.get(reverse("reservations:quote_detail", args=[reference]))
    html = response.content.decode()
    assert response.status_code == 200
    assert html.index('data-fast-apple-pay') < html.index('data-guest-journey')
    assert 'data-brands="APPLEPAY"' in html
    assert 'https://eu-test.oppwa.com/v1/paymentWidgets.js' in html
    assert "https://eu-test.oppwa.com" in response["Content-Security-Policy"]
    assert "payment=(self)" in response["Permissions-Policy"]
    assert BookingIntent.objects.filter(quote=quote).count() == 0


@override_settings(**UAT_SETTINGS)
def test_express_checkout_requires_consent_and_only_posts_quote_payment_data(monkeypatch):
    client, quote, reference = owned_quote()
    CheckoutClientStub.payloads = []
    monkeypatch.setattr(ApplePayFastCreateView, "client_class", CheckoutClientStub)
    monkeypatch.setattr(
        "apps.payments.views._fast_revalidate",
        lambda current: make_availability(current.property, total=Decimal("500.00")),
    )
    url = reverse("payments:apple_fast_create", args=[reference])
    assert client.post(url, {}).status_code == 400
    assert CheckoutClientStub.payloads == []
    response = create_checkout(client, reference)
    assert response.status_code == 200
    assert response.json()["checkoutId"] == "ABC123456789.uat01-vm-tx04"
    payload = CheckoutClientStub.payloads[0]
    assert payload["amount"] == "500.00"
    assert payload["currency"] == "SAR"
    assert all(not key.startswith(("customer.", "billing.")) for key in payload)
    assert BookingIntent.objects.filter(quote=quote).count() == 0
    assert PaymentAttempt.objects.count() == 0


@override_settings(**UAT_SETTINGS)
def test_wallet_authorization_creates_intent_then_reuses_existing_payment(monkeypatch):
    client, quote, reference = owned_quote()
    monkeypatch.setattr(ApplePayFastCreateView, "client_class", CheckoutClientStub)
    monkeypatch.setattr(
        "apps.payments.views._fast_revalidate",
        lambda current: make_availability(current.property, total=Decimal("500.00")),
    )
    checkout_id = create_checkout(client, reference).json()["checkoutId"]
    authorization = reverse("payments:apple_fast_authorize", args=[reference])
    wallet = wallet_contact()
    wallet["billingContact"]["addressLines"] = ["شارع غير مدعوم"]
    invalid = client.post(
        authorization,
        data=json.dumps({"checkoutId": checkout_id, "payment": wallet}),
        content_type="application/json",
    )
    assert invalid.status_code == 400
    assert BookingIntent.objects.filter(quote=quote).count() == 0

    wallet = wallet_contact()
    valid = client.post(
        authorization,
        data=json.dumps({"checkoutId": checkout_id, "payment": wallet}),
        content_type="application/json",
    )
    assert valid.status_code == 200
    assert valid.json() == {"ready": True}
    intent = BookingIntent.objects.get(quote=quote)
    assert intent.guest_email == "guest@example.invalid"
    attempt = PaymentAttempt.objects.get(booking_intent=intent)
    assert attempt.provider_checkout_id == checkout_id
    assert attempt.amount == Decimal("500.00")
    assert client.post(
        authorization,
        data=json.dumps({"checkoutId": checkout_id, "payment": wallet}),
        content_type="application/json",
    ).status_code == 200
    assert PaymentAttempt.objects.filter(booking_intent=intent).count() == 1


@override_settings(**UAT_SETTINGS)
def test_result_delegates_to_authoritative_verification(monkeypatch):
    client, quote, reference = owned_quote()
    monkeypatch.setattr(ApplePayFastCreateView, "client_class", CheckoutClientStub)
    monkeypatch.setattr(
        "apps.payments.views._fast_revalidate",
        lambda current: make_availability(current.property, total=Decimal("500.00")),
    )
    checkout_id = create_checkout(client, reference).json()["checkoutId"]
    client.post(
        reverse("payments:apple_fast_authorize", args=[reference]),
        data=json.dumps({"checkoutId": checkout_id, "payment": wallet_contact()}),
        content_type="application/json",
    )

    class VerificationStub:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def verify(self, attempt):
            return VerificationOutcome(attempt=attempt, status=HyperPayStatus.FAILED)

    monkeypatch.setattr(HyperPayResultView, "service_class", VerificationStub)
    result_url = reverse("payments:apple_fast_result")
    path = f"/v1/checkouts/{checkout_id}/payment"
    assert client.get(result_url, {"resourcePath": path}).status_code == 200
    assert client.get(result_url, {"resourcePath": "/v1/checkouts/other/payment"}).status_code == 404


@override_settings(**{**UAT_SETTINGS, "HYPERPAY_ENVIRONMENT": "production"})
def test_express_checkout_is_inaccessible_on_production_connection():
    client, _, reference = owned_quote()
    assert client.post(
        reverse("payments:apple_fast_create", args=[reference]),
        {"terms_accepted": "on", "privacy_accepted": "on"},
    ).status_code == 404
    page = client.get(reverse("reservations:quote_detail", args=[reference]))
    assert 'data-fast-apple-pay' not in page.content.decode()
