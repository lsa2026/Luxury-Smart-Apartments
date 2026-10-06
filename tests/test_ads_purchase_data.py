"""Synthetic opt-in and result-page checks; never contact Google or HyperPay."""

import hashlib
import json
from urllib.parse import quote

import pytest
from django.conf import settings
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from apps.core.enhanced_conversions import consented_purchase_email_hash
from apps.payments.hyperpay.result_codes import HyperPayStatus
from apps.payments.hyperpay.service import VerificationOutcome
from apps.payments.models import PaymentAttempt
from apps.payments.views import HyperPayResultView
from tests.test_booking_modifications_phase6 import confirmed_reservation
from tests.test_hyperpay import HYPERPAY_SETTINGS, ResultViewServiceStub, own_intent

GOOGLE = {
    "GOOGLE_INTEGRATIONS_ENABLED": True,
    "GOOGLE_TAG_MANAGER_ENABLED": True,
    "GOOGLE_ADS_ENABLED": True,
    "COOKIE_CONSENT_ENABLED": True,
}


def consent_cookie(**changes):
    return quote(
        json.dumps(
            {
                "version": settings.COOKIE_CONSENT_VERSION,
                "analytics": True,
                "marketing": True,
                "userProvidedData": True,
                "userProvidedDataVersion": 1,
                **changes,
            }
        )
    )


@override_settings(**GOOGLE)
def test_ads_hash_requires_current_independent_consent_and_valid_email():
    request = RequestFactory().get("/")
    assert consented_purchase_email_hash(request, "synthetic@example.invalid") == ""
    for changes in (
        {"analytics": False},
        {"marketing": False},
        {"userProvidedData": False},
        {"userProvidedDataVersion": 0},
        {"version": 999},
        {"userProvidedData": "true"},
        {"userProvidedDataVersion": True},
        {"version": True},
    ):
        request.COOKIES["lsa_cookie_consent"] = consent_cookie(**changes)
        assert consented_purchase_email_hash(request, "synthetic@example.invalid") == ""
    for malformed in ("{", "[]", "null"):
        request.COOKIES["lsa_cookie_consent"] = malformed
        assert consented_purchase_email_hash(request, "synthetic@example.invalid") == ""
    request.COOKIES["lsa_cookie_consent"] = consent_cookie()
    for email in ("", "broken", "x @gmail.com"):
        assert consented_purchase_email_hash(request, email) == ""
    assert consented_purchase_email_hash(request, " Synthetic.Guest@GMAIL.COM ") == (
        hashlib.sha256(b"syntheticguest@gmail.com").hexdigest()
    )
    for flag in GOOGLE:
        with override_settings(**{flag: False}):
            assert consented_purchase_email_hash(request, "synthetic@example.invalid") == ""


@pytest.mark.django_db
@override_settings(**HYPERPAY_SETTINGS, **GOOGLE)
@pytest.mark.parametrize("consented", [False, True])
def test_result_page_keeps_ads_identity_outside_purchase_payload(monkeypatch, consented):
    client = Client()
    reservation = confirmed_reservation()
    intent = reservation.booking_intent
    own_intent(client, intent)
    if consented:
        client.cookies["lsa_cookie_consent"] = consent_cookie()
    attempt = PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="hyperpay",
        provider_checkout_id="synthetic-ec",
        merchant_transaction_id="synthetic-ec",
        amount=intent.total_price,
        currency=intent.currency,
        status=PaymentAttempt.Status.SUCCEEDED,
        idempotency_key="synthetic-ec",
    )
    ResultViewServiceStub.outcome = VerificationOutcome(
        attempt,
        HyperPayStatus.SUCCESS,
        reservation=reservation,
    )
    monkeypatch.setattr(HyperPayResultView, "service_class", ResultViewServiceStub)
    response = client.get(reverse("payments:hyperpay_result", args=[attempt.pk]))
    content = response.content.decode()
    assert response.status_code == 200
    assert "data-analytics-purchase-event" in content
    assert ("data-ads-purchase-email-hash=" in content) is consented
    assert ("js/ads-purchase-data.js" in content) is consented
    assert intent.guest_email not in content
    assert "user_data" not in response.context["purchase_event"]
    assert "sha256_email_address" not in json.dumps(response.context["purchase_event"])
    assert response["Cache-Control"] == "no-store, private"
    if consented:
        assert content.index("js/ads-purchase-data.js") < content.index("js/analytics.js")
