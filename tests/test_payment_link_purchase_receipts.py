"""Future link captures -> clean receipt, with fake providers and no Google requests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection
from django.test import Client
from django.urls import reverse

from apps.core.models import MarketingEventReceipt
from apps.payments.hyperpay.result_codes import HyperPayStatus
from apps.payments.hyperpay.service import VerificationOutcome
from apps.payments.models import PaymentAttempt
from apps.payments.payment_links import issue_request
from apps.payments.purchase_receipts import SESSION_KEY, START_AT, receipt_for
from apps.payments.views import _hyperpay_return_token
from tests.test_payment_link_revisions import collect
from tests.test_reservation_payment_links import (
    isolated_settings,  # noqa: F401
    manual_stay,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def google_settings(settings, isolated_settings):  # noqa: F811
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.GOOGLE_INTEGRATIONS_ENABLED = True
    settings.GOOGLE_TAG_MANAGER_ENABLED = True
    settings.GOOGLE_TAG_MANAGER_CONTAINER_ID = "GTM-M9VLL75K"
    settings.GOOGLE_ANALYTICS_ENABLED = True
    settings.GOOGLE_ANALYTICS_MEASUREMENT_ID = "G-7E43GT39DN"
    settings.GOOGLE_ADS_ENABLED = True
    settings.GOOGLE_ADS_CONVERSION_ID = "AW-18090693214"
    settings.GOOGLE_CONSENT_MODE_ENABLED = True
    settings.GOOGLE_CONSENT_DEFAULT_ANALYTICS_STORAGE = "denied"
    settings.GOOGLE_CONSENT_DEFAULT_AD_STORAGE = "denied"
    settings.GOOGLE_CONSENT_DEFAULT_AD_USER_DATA = "denied"
    settings.GOOGLE_CONSENT_DEFAULT_AD_PERSONALIZATION = "denied"


def paid(*, amount="1250", status="succeeded", request_status="paid"):
    reservation = manual_stay()
    reservation.is_test = False
    reservation.save()
    request = issue_request(reservation.pk)
    request.amount_sar = Decimal(amount)
    request.status = request_status
    request.save()
    attempt = collect(reservation, amount, request=request, status=status)
    return reservation, request, attempt


def callback(client, attempt, status=HyperPayStatus.SUCCESS):
    url = reverse("payments:hyperpay_result", args=[attempt.pk])
    token = _hyperpay_return_token(attempt)
    outcome = VerificationOutcome(attempt, status, attempt.payment_request.reservation)
    with patch("apps.payments.views.HyperPayResultView.service_class") as service:
        service.return_value.__enter__.return_value.verify.return_value = outcome
        return client.get(
            url, {"return_token": token, "resourcePath": "/v1/checkouts/None/payment"}, secure=True
        )


def test_private_return_redirects_to_fixed_clean_receipt_and_uses_actual_capture():
    stay, request, attempt = paid(amount="500")
    client = Client()
    returned = callback(client, attempt)
    assert returned.status_code == 302
    assert returned.url == "/payments/receipt/"
    assert returned["Referrer-Policy"] == "no-referrer"
    page = client.get(returned.url, secure=True)
    assert page.status_code == 200
    event = page.context["purchase_event"]
    assert event["value"] == 500 and event["currency"] == "SAR"
    assert event["checkout_kind"] == "payment_link"
    assert event["items"][0]["price"] == 500
    assert event["transaction_id"] == MarketingEventReceipt.reference_hmac(
        f"payment-link-capture:{attempt.pk}"
    )
    html = page.content.decode()
    for secret in (
        str(attempt.pk),
        str(request.pk),
        stay.public_reference,
        stay.booking_intent.guest_email,
        stay.booking_intent.guest_phone,
        stay.booking_intent.guest_first_name,
        _hyperpay_return_token(attempt),
    ):
        assert secret not in html
    assert 'data-gtm-container-id="GTM-M9VLL75K"' in html
    assert 'data-ga4-measurement-id="G-7E43GT39DN"' in html
    assert 'data-google-ads-enabled="true"' in html
    assert 'data-consent-default-analytics="denied"' in html
    assert 'data-consent-default-ad="denied"' in html
    assert 'data-meta-measurement-page-allowed="false"' in html
    for forbidden in (
        "meta-measurement.js",
        "clarity",
        "ads-purchase-data.js",
        "data-ads-purchase-email-hash",
        "data-consent-banner",
        "<dialog",
    ):
        assert forbidden not in html
    assert "googletagmanager.com" in page["Content-Security-Policy"]
    assert "connect.facebook.net" not in page["Content-Security-Policy"]
    assert "no-store" in page["Cache-Control"]
    assert page["Referrer-Policy"] == "same-origin"


def test_query_and_sensitive_referrer_get_stripped_before_google_is_rendered():
    _, _, attempt = paid()
    client = Client()
    callback(client, attempt)
    page = client.get(
        "/payments/receipt/?payment_id=secret&gclid=untrusted",
        secure=True,
        HTTP_REFERER="https://testserver/payments/pay/private-token/?email=secret",
    )
    assert page.status_code == 302 and page.url == "/payments/receipt/"
    assert page["Referrer-Policy"] == "no-referrer" and not page.content
    assert client.get(page.url, secure=True).status_code == 200
    assert Client().get(page.url, secure=True).status_code == 404


def test_repeat_callback_and_reload_reuse_receipt_ack_then_never_emit_again():
    _, _, attempt = paid()
    client = Client(enforce_csrf_checks=True)
    first, second = callback(client, attempt), callback(client, attempt)
    assert first.url == second.url
    assert MarketingEventReceipt.objects.count() == 1
    page = client.get(first.url, secure=True)
    assert (
        client.get(first.url, secure=True).context["purchase_event"]
        == page.context["purchase_event"]
    )
    ack_url = reverse("marketing:purchase_acknowledge")
    data = {"receipt_token": page.context["purchase_receipt_token"]}
    assert client.post(ack_url, data, secure=True).status_code == 403
    token = page.context["purchase_receipt_csrf_token"]
    for _ in range(2):
        result = client.post(
            ack_url, data, secure=True, HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN="https://testserver"
        )
        assert result.status_code == 204
    assert MarketingEventReceipt.objects.get().status == "emitted"
    assert client.get(first.url, secure=True).context["purchase_event"] is None
    assert callback(client, attempt).status_code == 200
    assert b"data-analytics-purchase-event" not in callback(client, attempt).content


@pytest.mark.parametrize(
    "status", ["pending", "failed", "review", "unknown", "refunded", "partially_refunded"]
)
def test_non_successful_or_refunded_payments_never_prepare_purchase(status):
    _, _, attempt = paid(status=status)
    client = Client()
    assert callback(client, attempt).status_code == 200
    assert not MarketingEventReceipt.objects.exists()
    assert SESSION_KEY not in client.session


@pytest.mark.parametrize(
    "bad",
    ["old", "unverified", "missing_id", "request_review", "outcome_review", "test", "wrong_amount"],
)
def test_historical_or_inconsistent_payments_never_prepare_purchase(bad):
    stay, request, attempt = paid()
    if bad == "old":
        PaymentAttempt.objects.filter(pk=attempt.pk).update(created_at=START_AT - timedelta(days=1))
    elif bad == "unverified":
        attempt.verified_at = None
        attempt.save()
    elif bad == "missing_id":
        attempt.provider_payment_id = None
        attempt.save()
    elif bad == "request_review":
        request.status = "review"
        request.save()
    elif bad == "test":
        stay.is_test = True
        stay.save()
    elif bad == "wrong_amount":
        request.amount_sar += Decimal("1")
        request.save()
    attempt.refresh_from_db()
    client = Client()
    result = callback(
        client,
        attempt,
        HyperPayStatus.REVIEW if bad == "outcome_review" else HyperPayStatus.SUCCESS,
    )
    assert result.status_code == 200
    assert not MarketingEventReceipt.objects.exists()


def test_remaining_payment_has_its_own_transaction_not_the_booking_total():
    _, request, first = paid(amount="500")
    # Another successful capture for the same booking has a distinct receipt,
    # even if it collects the remaining amount rather than the full order.
    second = collect(request.reservation, "750", request=request)
    assert receipt_for(first).object_reference_hash != receipt_for(second).object_reference_hash


def test_amended_remaining_balance_measures_capture_not_new_total(settings):
    from apps.payments.payment_link_revisions import prepare_preview
    from apps.payments.payment_links import complete_request_payment
    from tests.test_payment_link_revisions import activate, change

    settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED = True
    settings.HOSTAWAY_LIVE_EXTENSION_ENABLED = True
    stay = manual_stay()
    stay.is_test = False
    stay.save()
    collect(stay, "500")
    modification = change(stay)
    request, _ = activate(prepare_preview(stay.pk, modification.pk))
    assert request.amount_sar == Decimal("1642")
    attempt = collect(stay, "1642", request=request)
    complete_request_payment(attempt)
    request.refresh_from_db()
    assert request.status == "paid"
    client = Client()
    response = callback(client, attempt)
    page = client.get(response.url, secure=True)
    assert page.context["purchase_event"]["value"] == 1642
    assert page.context["purchase_event"]["value"] != float(modification.new_total)


def test_secondary_measurement_failure_keeps_private_paid_result_available():
    _, _, attempt = paid()
    with patch("apps.payments.purchase_receipts.receipt_for", side_effect=DatabaseError()):
        response = callback(Client(), attempt)
    assert response.status_code == 200
    assert b"data-analytics-purchase-event" not in response.content
    attempt.refresh_from_db()
    assert attempt.status == "succeeded"


@pytest.mark.parametrize(
    "issued", ["2000-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00", "invalid"]
)
def test_invalid_or_expired_receipt_session_never_loads_trackers(issued):
    _, _, attempt = paid()
    client = Client()
    callback(client, attempt)
    session = client.session
    session[SESSION_KEY] = {"attempt": str(attempt.pk), "issued_at": issued}
    session.save()
    response = client.get("/payments/receipt/", secure=True)
    assert response.status_code == 404
    assert b"data-analytics-purchase-event" not in response.content


def test_clean_receipt_rechecks_refund_and_expired_session_grants():
    _, _, attempt = paid()
    client = Client()
    callback(client, attempt)
    attempt.status = "refunded"
    attempt.save()
    assert client.get("/payments/receipt/", secure=True).status_code == 404


def test_real_gateway_verification_completes_payment_before_clean_receipt():
    from apps.payments.hyperpay.service import HyperPayService
    from tests.test_hyperpay import HyperPayStub
    from tests.test_reservation_payment_links import approved_document, checkout

    stay = manual_stay()
    stay.is_test = False
    stay.save()
    request = issue_request(stay.pk)
    provider = HyperPayStub()
    attempt = checkout(request, stub=provider).attempt
    provider.payment_document = approved_document(attempt)
    url = reverse("payments:hyperpay_result", args=[attempt.pk])
    with patch("apps.payments.views.HyperPayResultView.service_class") as service:
        service.return_value = HyperPayService(client=provider)
        response = Client().get(url, {"return_token": _hyperpay_return_token(attempt)}, secure=True)
    request.refresh_from_db()
    attempt.refresh_from_db()
    assert request.status == "paid" and attempt.status == "succeeded"
    assert response.status_code == 302 and response.url == "/payments/receipt/"
    assert MarketingEventReceipt.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_callbacks_share_one_receipt_and_transaction_id():
    if connection.vendor != "postgresql":
        pytest.skip("Concurrent receipt preparation requires PostgreSQL")
    _, _, attempt = paid()
    barrier = Barrier(2)
    url = reverse("payments:hyperpay_result", args=[attempt.pk])

    def run():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            response = Client().get(
                url, {"return_token": _hyperpay_return_token(attempt)}, secure=True
            )
            return response.status_code
        finally:
            close_old_connections()

    with patch("apps.payments.views.HyperPayResultView.service_class") as service:
        service.return_value.__enter__.return_value.verify.return_value = VerificationOutcome(
            attempt, HyperPayStatus.SUCCESS, attempt.payment_request.reservation
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(lambda _: run(), range(2))) == [302, 302]
    assert MarketingEventReceipt.objects.count() == 1
