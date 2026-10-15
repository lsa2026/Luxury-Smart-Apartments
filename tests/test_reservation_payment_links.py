"""Existing-booking collection: disposable DB, fake providers, no live side effects."""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.core.guest_documents import documents_digest
from apps.payments.currency import CurrencyService
from apps.payments.hyperpay.exceptions import HyperPayCheckoutError, HyperPayConnectionError
from apps.payments.hyperpay.service import HyperPayService
from apps.payments.models import PaymentAttempt, ReservationPaymentRequest
from apps.payments.payment_link_tasks import (
    deliver_payment_request_task,
    queue_delivery,
)
from apps.payments.payment_links import (
    SALT,
    create_request_checkout,
    issue_request,
    link_token,
    public_url,
    reconcile_requests,
    resolve_token,
)
from apps.reservations.models import BookingIntent, ManualBookingDraft, Reservation
from apps.reservations.services.hostaway_booking import prepare_local_reservation
from tests.test_currency import fixed_rates
from tests.test_hyperpay import HYPERPAY_SETTINGS, HyperPayStub, payable_intent
from tests.test_manual_booking_operations_phase61 import owner

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_settings(settings):
    for key, value in HYPERPAY_SETTINGS.items():
        setattr(settings, key, value)
    settings.RESERVATION_PAYMENT_LINKS_ENABLED = True
    settings.SITE_BASE_URL = "https://booking.example"
    settings.GOOGLE_INTEGRATIONS_ENABLED = True
    cache.clear()
    # Fail the test, rather than ever contacting a real provider by accident.
    with patch("httpx.Client.request", side_effect=AssertionError("unexpected network request")):
        yield


def manual_stay(currency="SAR", language="ar"):
    intent = payable_intent()
    intent.language = language
    intent.expires_at = timezone.now() + timedelta(days=5)
    intent.guest_phone = "+16502530000"
    if currency == "MAD":
        intent.currency = currency
        with (
            CurrencyService() as service,
            patch.object(service, "get_rates", return_value=fixed_rates()),
        ):
            quoted = service.create_quote(
                source_amount=intent.total_price,
                source_currency="MAD",
                display_currency="MAD",
                quote_created_at=timezone.now(),
                quote_expires_at=intent.expires_at,
            )
        intent.payment_amount_sar = quoted.payment_amount_sar
        intent.exchange_rate_snapshot = dict(quoted.snapshot)
    intent.save()
    ManualBookingDraft.objects.create(
        quote=intent.quote,
        property=intent.property,
        check_in=intent.check_in,
        check_out=intent.check_out,
        nights=intent.nights,
        guests=intent.guests,
        currency=currency,
        system_total_price=intent.quote.total_price,
        final_total_price=intent.total_price,
        status="booked_awaiting_payment",
        availability_checked_at=timezone.now(),
        expires_at=intent.expires_at,
    )
    reservation = prepare_local_reservation(intent)
    reservation.normalized_status = "confirmed"
    reservation.hostaway_reservation_id = 97001
    reservation.hostaway_listing_map_id = intent.property.hostaway_listing_map_id
    reservation.is_test = True
    reservation.payment_status = "unpaid"
    reservation.save()
    return reservation


def remote(reservation):
    client = Mock()
    client.get_reservation.return_value = SimpleNamespace(
        reservation_id=reservation.hostaway_reservation_id,
        listing_map_id=reservation.hostaway_listing_map_id,
        status="new",
        check_in=reservation.check_in,
        check_out=reservation.check_out,
        guests=reservation.guests,
        currency=reservation.currency,
        total_price=reservation.total_price,
        payment_status="unpaid",
    )
    client.get_guest_charges.return_value = []
    return client


def billing():
    return {
        "billing_street1": "King Road 10",
        "billing_city": "Riyadh",
        "billing_state": "Riyadh",
        "billing_country": "SA",
        "billing_postcode": "12345",
    }


def form_data():
    return billing() | {
        "terms_accepted": "on",
        "house_rules_accepted": "on",
        "documents_digest": documents_digest(),
    }


def checkout(payment_request, stub=None, hostaway=None):
    return create_request_checkout(
        payment_request.pk,
        billing_data=billing(),
        acceptance={
            "consents": {"terms": True, "house_rules": True},
            "documents_digest": documents_digest(),
        },
        client=stub or HyperPayStub(),
        hostaway=hostaway or remote(payment_request.reservation),
    )


def approved_document(attempt, **overrides):
    return {
        "id": "verified-payment-12345678",
        "result": {"code": "000.000.000"},
        "amount": str(attempt.amount),
        "currency": "SAR",
        "paymentType": "DB",
        "merchantTransactionId": attempt.merchant_transaction_id,
        "paymentBrand": "VISA",
        **overrides,
    }


def page_url(payment_request):
    return reverse("payments:reservation_payment", args=[link_token(payment_request)])


def test_issue_is_idempotent_and_token_is_payment_only():
    stay = manual_stay()
    request = issue_request(stay.pk)
    assert issue_request(stay.pk).pk == request.pk
    assert link_token(request) == link_token(request)
    assert resolve_token(link_token(request)).pk == request.pk
    assert "booking.example/payments/pay/" in public_url(request)
    assert stay.booking_intent.guest_email not in public_url(request)
    assert resolve_token(link_token(request) + "tampered") is None
    malformed = signing.Signer(salt=SALT).sign_object({"request": "invalid-uuid"})
    assert resolve_token(malformed) is None
    assert Reservation.objects.count() == BookingIntent.objects.count() == 1
    assert PaymentAttempt.objects.count() == 0


def test_database_disallows_two_open_requests():
    request = issue_request(manual_stay().pk)
    with pytest.raises(IntegrityError), transaction.atomic():
        ReservationPaymentRequest.objects.create(
            reservation=request.reservation,
            amount_sar=10,
            source_amount=10,
            source_currency="SAR",
            expires_at=request.expires_at,
        )


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_preview_never_contacts_provider_or_creates_payment(language):
    stay = manual_stay(language=language)
    request = issue_request(stay.pk)
    client = Client()
    response = client.get(page_url(request))
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "same-origin"
    assert "noindex" in response["X-Robots-Tag"]
    assert b"googletagmanager.com" not in response.content
    assert b"paymentWidgets.js" not in response.content
    assert stay.booking_intent.guest_email.encode() not in response.content
    assert PaymentAttempt.objects.count() == 0
    assert client.get(reverse("reservations:manage", args=[stay.public_reference])).status_code in {
        302,
        403,
        404,
    }


@pytest.mark.parametrize("status", ["revoked", "review", "paid"])
def test_closed_link_does_not_offer_checkout(status):
    request = issue_request(manual_stay().pk)
    request.status = status
    request.save()
    response = Client().get(page_url(request))
    assert response.status_code == 200
    assert b'name="billing_city"' not in response.content
    assert PaymentAttempt.objects.count() == 0


def test_expiry_closes_link_but_preserves_booking():
    stay = manual_stay()
    request = issue_request(stay.pk)
    request.expires_at = timezone.now() - timedelta(seconds=1)
    request.save()
    assert b'name="billing_city"' not in Client().get(page_url(request)).content
    with pytest.raises(HyperPayCheckoutError):
        checkout(request)
    stay.refresh_from_db()
    assert stay.normalized_status == "confirmed"
    assert issue_request(stay.pk).pk != request.pk


@pytest.mark.parametrize(
    "change,value",
    [
        ("normalized_status", "cancelled"),
        ("payment_status", "paid"),
        ("total_price", Decimal("987")),
        ("guests", 3),
        ("is_test", False),
    ],
)
def test_changed_cancelled_or_paid_booking_cannot_charge(change, value):
    stay = manual_stay()
    request = issue_request(stay.pk)
    setattr(stay, change, value)
    stay.save()
    with pytest.raises((HyperPayCheckoutError, ValueError)):
        checkout(request)
    assert PaymentAttempt.objects.count() == 0


def test_remote_cancelled_booking_and_offline_payment_block_checkout():
    stay = manual_stay()
    request = issue_request(stay.pk)
    hostaway = remote(stay)
    hostaway.get_reservation.return_value.status = "cancelled"
    with pytest.raises(HyperPayCheckoutError):
        checkout(request, hostaway=hostaway)
    hostaway = remote(stay)
    hostaway.get_guest_charges.return_value = [{"status": "paid", "amount": "1"}]
    with pytest.raises(HyperPayCheckoutError):
        checkout(request, hostaway=hostaway)
    assert PaymentAttempt.objects.count() == 0


@pytest.mark.parametrize("currency", ["SAR", "MAD"])
def test_checkout_keeps_final_price_skips_availability_and_reuses_attempt(currency):
    stay = manual_stay(currency=currency)
    request = issue_request(stay.pk)
    stub = HyperPayStub()
    with patch("apps.reservations.services.availability.AvailabilityService.check") as availability:
        first = checkout(request, stub=stub)
        second = checkout(request, stub=stub)
    availability.assert_not_called()
    assert first.attempt.pk == second.attempt.pk
    assert stub.checkout_calls == 1
    assert stub.payload["currency"] == "SAR"
    assert Decimal(stub.payload["amount"]) == request.amount_sar
    assert first.attempt.payment_request_id == request.pk
    assert request.source_currency == currency
    assert Reservation.objects.count() == BookingIntent.objects.count() == 1


def test_uncertain_checkout_is_durable_and_blocks_retry():
    request = issue_request(manual_stay().pk)
    stub = HyperPayStub()
    stub.create_checkout = Mock(side_effect=HyperPayConnectionError())
    with pytest.raises(HyperPayCheckoutError, match="uncertain"):
        checkout(request, stub=stub)
    assert request.attempts.get().status == "review"
    with pytest.raises(HyperPayCheckoutError, match="unresolved"):
        checkout(request, stub=stub)
    assert stub.create_checkout.call_count == 1


def test_expired_pending_attempt_cannot_be_replaced():
    stay = manual_stay()
    request = issue_request(stay.pk)
    checkout(request)
    request.expires_at = timezone.now() - timedelta(seconds=1)
    request.save()
    with pytest.raises(HyperPayCheckoutError, match="unresolved"):
        issue_request(stay.pk)
    assert stay.payment_requests.count() == 1


def test_success_updates_existing_booking_without_inventory_write_or_management_access():
    stay = manual_stay()
    request = issue_request(stay.pk)
    attempt = checkout(request).attempt
    stub = HyperPayStub()
    stub.payment_document = approved_document(attempt)
    with patch("apps.payments.hyperpay.service.HostawayBookingService") as booking:
        outcome = HyperPayService(client=stub).verify(attempt)
        again = HyperPayService(client=stub).verify(attempt)
    booking.assert_not_called()
    assert outcome.reservation.pk == again.reservation.pk == stay.pk
    request.refresh_from_db()
    stay.refresh_from_db()
    assert request.status == "paid" and request.paid_at
    assert stay.payment_status == "paid" and stay.hostaway_reservation_id == 97001
    assert Reservation.objects.count() == 1
    from apps.payments.views import _hyperpay_return_token

    attempt.refresh_from_db()
    client = Client()
    with patch("apps.payments.views.HyperPayService", return_value=HyperPayService(client=stub)):
        response = client.get(
            reverse("payments:hyperpay_result", args=[attempt.pk]),
            {"return_token": _hyperpay_return_token(attempt)},
        )
    assert response.status_code == 200
    assert response["Referrer-Policy"] == "no-referrer"
    assert b"googletagmanager.com" not in response.content
    assert "claimable_reference" not in response.context
    assert client.get(reverse("reservations:manage", args=[stay.public_reference])).status_code in {
        302,
        403,
        404,
    }


@pytest.mark.parametrize(
    "override",
    [
        {"amount": "1"},
        {"currency": "USD"},
        {"paymentType": "PA"},
        {"merchantTransactionId": "different"},
        {"paymentBrand": "NOT_ALLOWED"},
        {"id": ""},
    ],
)
def test_mismatched_gateway_response_never_marks_paid(override):
    stay = manual_stay()
    request = issue_request(stay.pk)
    attempt = checkout(request).attempt
    stub = HyperPayStub()
    stub.payment_document = approved_document(attempt, **override)
    assert HyperPayService(client=stub).verify(attempt).attempt.status == "review"
    stay.refresh_from_db()
    assert stay.payment_status == "unpaid"


def test_paid_after_local_cancellation_records_financial_success_for_review_only():
    stay = manual_stay()
    request = issue_request(stay.pk)
    attempt = checkout(request).attempt
    stay.normalized_status = "cancelled"
    stay.save()
    stub = HyperPayStub()
    stub.payment_document = approved_document(attempt)
    HyperPayService(client=stub).verify(attempt)
    request.refresh_from_db()
    stay.refresh_from_db()
    assert request.status == "review"
    assert request.attempts.get().status == "succeeded"
    assert stay.normalized_status == "cancelled" and stay.payment_status == "unpaid"


def test_recovery_checks_payment_after_browser_closed_even_if_link_expired(settings):
    request = issue_request(manual_stay().pk)
    attempt = checkout(request).attempt
    request.expires_at = timezone.now() - timedelta(minutes=1)
    request.save()
    settings.RESERVATION_PAYMENT_LINKS_ENABLED = False
    stub = HyperPayStub()
    stub.payment_document = approved_document(attempt)
    with patch(
        "apps.payments.payment_links.HyperPayService", return_value=HyperPayService(client=stub)
    ):
        assert reconcile_requests()["checked"] == 1
    request.refresh_from_db()
    assert request.status == "paid"


def test_pending_gateway_response_without_financial_fields_stays_unresolved():
    request = issue_request(manual_stay().pk)
    attempt = checkout(request).attempt
    stub = HyperPayStub()
    stub.payment_document = {"result": {"code": "000.200.100"}}
    HyperPayService(client=stub).verify(attempt)
    attempt.refresh_from_db()
    assert attempt.status == "pending" and not attempt.verified_at


@pytest.mark.parametrize(
    "field",
    [
        "terms_accepted",
        "house_rules_accepted",
        "documents_digest",
        "billing_city",
        "billing_postcode",
    ],
)
def test_form_requires_guest_acceptance_and_real_billing(field):
    request = issue_request(manual_stay().pk)
    data = form_data()
    data.pop(field)
    response = Client().post(page_url(request), data)
    assert response.status_code == 200
    assert PaymentAttempt.objects.count() == 0


def test_cross_session_guest_post_and_csp_and_amount_tampering():
    stay = manual_stay()
    request = issue_request(stay.pk)
    data = form_data() | {"amount": "1", "currency": "USD", "reservation": "other"}
    stub, hostaway = HyperPayStub(), remote(stay)
    with patch("apps.payments.payment_links.HostawayClient") as cls:
        cls.return_value.__enter__.return_value = hostaway
        with patch(
            "apps.payments.payment_links.HyperPayService", return_value=HyperPayService(client=stub)
        ):
            response = Client().post(page_url(request), data)
    assert response.status_code == 200
    assert b"paymentWidgets.js" in response.content
    assert "eu-test.oppwa.com" in response["Content-Security-Policy"]
    assert "payment=(self)" in response["Permissions-Policy"]
    assert response["Referrer-Policy"] == "same-origin"
    assert Decimal(stub.payload["amount"]) == request.amount_sar
    assert stay.booking_intent.guest_email.encode() not in response.content
    request.refresh_from_db()
    assert request.legal_acceptance["source"] == "reservation_payment_link"
    assert request.legal_acceptance["rate_conditions"]["total"] == str(request.source_amount)


def test_csrf_required_and_feature_off_hides_page(settings):
    request = issue_request(manual_stay().pk)
    assert Client(enforce_csrf_checks=True).post(page_url(request), form_data()).status_code == 403
    settings.RESERVATION_PAYMENT_LINKS_ENABLED = False
    assert Client().get(page_url(request)).status_code == 404


@pytest.mark.parametrize("origin", ["https://testserver", None])
def test_secure_payment_form_passes_real_csrf_then_renders_gateway(origin):
    payment_request = issue_request(manual_stay().pk)
    client = Client(enforce_csrf_checks=True)
    page = client.get(page_url(payment_request), secure=True)
    assert page["Referrer-Policy"] == "same-origin"
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())[1]
    headers = (
        {"HTTP_ORIGIN": origin}
        if origin
        else {"HTTP_REFERER": "https://testserver" + page_url(payment_request)}
    )
    stub = HyperPayStub()
    with (
        patch("apps.payments.payment_links.HostawayClient") as hostaway,
        patch(
            "apps.payments.payment_links.HyperPayService", return_value=HyperPayService(client=stub)
        ),
    ):
        hostaway.return_value.__enter__.return_value = remote(payment_request.reservation)
        result = client.post(
            page_url(payment_request),
            form_data() | {"csrfmiddlewaretoken": csrf},
            secure=True,
            **headers,
        )
    assert result.status_code == 200
    assert b"paymentWidgets.js" in result.content
    assert stub.checkout_calls == 1


@pytest.mark.parametrize("origin", ["null", "https://untrusted.example"])
def test_null_or_cross_origin_is_still_rejected_even_with_valid_csrf(origin):
    payment_request = issue_request(manual_stay().pk)
    client = Client(enforce_csrf_checks=True)
    page = client.get(page_url(payment_request), secure=True)
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())[1]
    result = client.post(
        page_url(payment_request),
        form_data() | {"csrfmiddlewaretoken": csrf},
        secure=True,
        HTTP_ORIGIN=origin,
    )
    assert result.status_code == 403
    assert not PaymentAttempt.objects.exists()


def test_payment_address_reuses_shared_ui_and_proxy_with_browser_scoped_tickets(settings):
    settings.GOOGLE_PLACES_ENABLED = True
    settings.GOOGLE_PLACES_API_KEY = "synthetic-key"
    settings.GOOGLE_PLACES_REQUIRE_SHARED_CACHE = False
    request = issue_request(manual_stay(language="fr").pk)
    quote = request.reservation.booking_intent.quote
    quote.status = "consumed"
    quote.expires_at = timezone.now() - timedelta(hours=1)
    quote.save(update_fields=["status", "expires_at"])
    client = Client(enforce_csrf_checks=True)
    page = client.get(page_url(request), secure=True)
    html = page.content.decode()
    assert "reservations/_billing_fields.html" in [t.name for t in page.templates]
    assert "data-checkout-address" in html and "checkout-address.js" in html
    assert "maps.googleapis.com" not in html and "googletagmanager.com" not in html
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)[1]
    ticket = page.context["address_places_ticket"]
    endpoint = page.context["address_suggestions_url"]
    headers = {
        "secure": True,
        "HTTP_ORIGIN": "https://testserver",
        "HTTP_X_CSRFTOKEN": csrf,
        "HTTP_ACCEPT_LANGUAGE": "ar",
    }
    payload = {"ticket": ticket, "input": "King Street"}
    assert (
        client.post(
            endpoint,
            payload,
            content_type="application/json",
            secure=True,
            HTTP_ORIGIN="https://testserver",
        ).status_code
        == 403
    )
    with patch("apps.reservations.address_places.httpx.Client") as factory:
        google = factory.return_value.__enter__.return_value
        google.post.return_value.json.return_value = {
            "suggestions": [
                {"placePrediction": {"placeId": "ChIJ-example", "text": {"text": "King Street"}}}
            ]
        }
        result = client.post(endpoint, payload, content_type="application/json", **headers)
        assert result.status_code == 200 and result.json()["suggestions"]
        assert google.post.call_args.kwargs["json"]["languageCode"] == "fr"
        assert "Referer" not in google.post.call_args.kwargs["headers"]
        assert "ticket" not in google.post.call_args.kwargs["json"]
        assert "payment:" not in str(google.post.call_args)
        # Selection uses exactly the ordinary mapping and rotates its ticket.
        google.get.return_value.json.return_value = {
            "addressComponents": [
                {"types": ["country"], "longText": "Saudi Arabia", "shortText": "SA"}
            ]
        }
        details = client.post(
            page.context["address_details_url"],
            {"ticket": ticket, "place_id": "ChIJ-example"},
            content_type="application/json",
            **headers,
        )
        assert details.status_code == 200 and details.json()["fields"] == {"billing_country": "SA"}
        assert details.json()["next_ticket"] != ticket
        renewed_payload = payload | {"ticket": details.json()["next_ticket"]}
        assert (
            client.post(
                endpoint, renewed_payload, content_type="application/json", **headers
            ).status_code
            == 200
        )
        assert google.post.call_args.kwargs["json"]["languageCode"] == "fr"
        with patch(
            "apps.reservations.address_places.signing.loads", side_effect=signing.SignatureExpired
        ):
            expired = client.post(
                endpoint, renewed_payload, content_type="application/json", **headers
            )
        assert expired.status_code == 409
        renewed_payload["ticket"] = expired.json()["next_ticket"]
        assert (
            client.post(
                endpoint, renewed_payload, content_type="application/json", **headers
            ).status_code
            == 200
        )
        assert google.post.call_args.kwargs["json"]["languageCode"] == "fr"
        # An unrelated browser cannot reuse even a copied short-lived ticket.
        another = Client()
        another.get(page_url(request))
        assert another.post(endpoint, payload, content_type="application/json").status_code == 400
        request.status = "revoked"
        request.save()
        assert (
            client.post(endpoint, payload, content_type="application/json", **headers).status_code
            == 404
        )
        assert google.post.call_count == 3 and google.get.call_count == 1
    assert not PaymentAttempt.objects.exists()
    assert client.get(
        reverse("reservations:manage", args=[request.reservation.public_reference])
    ).status_code in {302, 403, 404}


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_branded_payment_text_and_owner_copy_are_separate(language, settings):
    from apps.payments.payment_link_tasks import payment_message

    request = issue_request(manual_stay(language=language).pk)
    settings.HYPERPAY_ENVIRONMENT = "production"
    text = payment_message(request)
    owner_copy = payment_message(request, owner_review=True)
    assert text in owner_copy and owner_copy != text
    assert text.startswith("✨ *Luxury Smart Apartments*\n\n")
    assert "Luxury Living" not in text and "لاكشري ليفينغ" not in text
    assert all(icon in text for icon in ("📅", "💳", "🔗"))
    assert "✅" not in text  # An unpaid link must not imply a successful payment.
    assert public_url(request) in text and "\n\n" in text
    assert request.reservation.public_reference not in text
    receipt = payment_message(request, "receipt")
    assert public_url(request) not in receipt
    assert "✅" in receipt and "🔗" not in receipt
    assert "Luxury Smart Apartments" in receipt
    if language == "ar":
        assert "ريال سعودي" in text
        assert "للمالك" not in text and "للمالك" in owner_copy


def test_whatsapp_delivery_is_durable_and_not_repeated(settings):
    settings.ULTRAMSG_ENABLED = True
    request = issue_request(manual_stay().pk)
    delivery = queue_delivery(request.pk)
    assert queue_delivery(request.pk).pk == delivery.pk
    fake = Mock()
    fake.send_text.return_value = {"sent": "true", "id": "test-message-1"}
    with patch("apps.payments.payment_link_tasks.UltraMsgClient") as cls:
        cls.return_value.__enter__.return_value = fake
        assert deliver_payment_request_task.run(delivery.pk)["status"] == "sent"
        assert deliver_payment_request_task.run(delivery.pk)["status"] == "sent"
    fake.send_text.assert_called_once()
    assert (
        fake.send_text.call_args.kwargs["recipient"]
        == request.reservation.booking_intent.guest_phone
    )
    assert public_url(request) in fake.send_text.call_args.kwargs["body"]
    assert request.deliveries.count() == 1
    resent = queue_delivery(request.pk, resend=True)
    assert resent.pk != delivery.pk and resent.sequence == 2
    assert request.deliveries.count() == 2
    assert queue_delivery(request.pk).pk == resent.pk


def test_whatsapp_timeout_never_blindly_retries(settings):
    settings.ULTRAMSG_ENABLED = True
    request = issue_request(manual_stay().pk)
    delivery = queue_delivery(request.pk)
    with patch("apps.payments.payment_link_tasks.UltraMsgClient") as cls:
        cls.return_value.__enter__.return_value.send_text.side_effect = TimeoutError()
        assert deliver_payment_request_task.run(delivery.pk)["status"] == "unknown"
        assert deliver_payment_request_task.run(delivery.pk)["status"] == "unknown"
        assert cls.return_value.__enter__.return_value.send_text.call_count == 1
    assert queue_delivery(request.pk).pk == delivery.pk
    resent = queue_delivery(request.pk, resend=True)
    assert resent.pk != delivery.pk and resent.status == "queued"


def test_owner_action_does_not_allow_anonymous_and_redirects_owner():
    stay = manual_stay()
    url = reverse("notifications:booking_detail", args=[stay.pk])
    client = Client()
    assert client.post(url, {"action": "send_guest_payment_link"}).status_code in {302, 403, 404}
    assert not stay.payment_requests.exists()
    client.force_login(owner())
    response = client.post(url, {"action": "send_guest_payment_link"})
    assert response.status_code == 302
    assert response.url == url
    assert stay.payment_requests.get().deliveries.count() == 1


def accounting_sender(settings):
    settings.ULTRAMSG_ENABLED = True
    settings.ACCOUNTING_WHATSAPP_NUMBER = "+966500000001"
    client = Mock()
    client.send_text.return_value = {"sent": "true", "id": "fake-accounting-message"}
    return client


def test_optional_accounting_button_is_read_only_and_owner_only():
    from apps.notifications.models import WhatsAppDelivery

    stay, client = manual_stay(), Client()
    url = reverse("notifications:booking_detail", args=[stay.pk])
    assert client.post(url, {"action": "request_accounting_payment_link"}).status_code in {
        302,
        403,
        404,
    }
    assert not WhatsAppDelivery.objects.exists()
    actor = owner()
    client.force_login(actor)
    html = client.get(url).content.decode()
    assert 'value="request_accounting_payment_link"' in html
    assert 'value="send_guest_payment_link"' in html
    assert "اختياري فقط" in html
    assert not WhatsAppDelivery.objects.exists()
    assert not stay.payment_requests.exists()
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(actor)
    assert csrf_client.post(url, {"action": "request_accounting_payment_link"}).status_code == 403
    assert not WhatsAppDelivery.objects.exists()


def test_optional_accounting_sends_only_to_accountant_and_deduplicates(settings):
    from apps.notifications.models import WhatsAppDelivery

    stay, client = manual_stay(), Client()
    client.force_login(owner())
    fake = accounting_sender(settings)
    url = reverse("notifications:booking_detail", args=[stay.pk])
    with patch("apps.notifications.services.ultramsg.UltraMsgClient") as cls:
        cls.return_value.__enter__.return_value = fake
        for _ in range(2):
            response = client.post(url, {"action": "request_accounting_payment_link"})
            assert response.status_code == 302 and response.url == url
    fake.send_text.assert_called_once()
    assert fake.send_text.call_args.kwargs["recipient"] == settings.ACCOUNTING_WHATSAPP_NUMBER
    assert WhatsAppDelivery.objects.get(reservation=stay).status == "sent"
    assert not stay.payment_requests.exists()
    assert not PaymentAttempt.objects.exists()
    stay.refresh_from_db()
    assert stay.payment_status == "unpaid" and stay.normalized_status == "confirmed"
    with pytest.raises(HyperPayCheckoutError, match="accounting_collection_requires_review"):
        issue_request(stay.pk)


@pytest.mark.parametrize("status", ["created", "pending", "unknown", "review", "succeeded"])
def test_optional_accounting_blocks_gateway_attempt_even_after_link_revocation(status, settings):
    from apps.notifications.models import WhatsAppDelivery
    from apps.notifications.services.ultramsg import send_manual_payment_link_request

    stay = manual_stay()
    request = issue_request(stay.pk)
    attempt = checkout(request).attempt
    attempt.status = status
    attempt.save(update_fields=["status"])
    request.status = "revoked"
    request.save(update_fields=["status"])
    accounting_sender(settings)
    with patch("apps.notifications.services.ultramsg.UltraMsgClient") as messaging:
        with pytest.raises(HyperPayCheckoutError, match="previous_collection_requires_review"):
            send_manual_payment_link_request(reservation_id=stay.pk, owner_optional=True)
        messaging.assert_not_called()
    assert not WhatsAppDelivery.objects.exists()


def test_optional_accounting_requires_explicit_site_link_stop(settings):
    from apps.notifications.models import WhatsAppDelivery

    stay, client = manual_stay(), Client()
    request = issue_request(stay.pk)
    client.force_login(owner())
    fake = accounting_sender(settings)
    url = reverse("notifications:booking_detail", args=[stay.pk])
    with patch("apps.notifications.services.ultramsg.UltraMsgClient") as cls:
        cls.return_value.__enter__.return_value = fake
        client.post(url, {"action": "request_accounting_payment_link"})
        assert not WhatsAppDelivery.objects.exists()
        fake.send_text.assert_not_called()
        request.refresh_from_db()
        assert request.status == "open"
        client.post(url, {"action": "revoke_guest_payment_link"})
        client.post(url, {"action": "request_accounting_payment_link"})
    request.refresh_from_db()
    assert request.status == "revoked"
    fake.send_text.assert_called_once()
    assert WhatsAppDelivery.objects.get(reservation=stay).status == "sent"


@pytest.mark.parametrize("payment_status", ["paid", "fully_paid", "partially_paid"])
def test_optional_accounting_never_sends_for_already_collected_booking(payment_status, settings):
    from apps.notifications.models import WhatsAppDelivery
    from apps.notifications.services.ultramsg import send_manual_payment_link_request

    stay = manual_stay()
    stay.payment_status = payment_status
    stay.save(update_fields=["payment_status"])
    accounting_sender(settings)
    with pytest.raises(HyperPayCheckoutError, match="existing_collection_requires_review"):
        send_manual_payment_link_request(reservation_id=stay.pk, owner_optional=True)
    assert not WhatsAppDelivery.objects.exists()


def test_new_manual_booking_offers_accounting_as_alternative_not_automatic(settings):
    from apps.notifications.models import WhatsAppDelivery

    stay, client = manual_stay(), Client()
    draft = ManualBookingDraft.objects.get(quote=stay.booking_intent.quote)
    draft.status = "ready_for_payment"
    draft.save(update_fields=["status"])
    client.force_login(owner())
    url = reverse("notifications:manual_booking_detail", args=[draft.pk])
    html = client.get(url).content.decode()
    assert 'value="create_hostaway_and_request_accounting_link"' in html
    assert 'value="create_hostaway_and_request_payment"' in html
    fake = accounting_sender(settings)
    with (
        patch("apps.reservations.operations_views.create_manual_booking_in_hostaway") as create,
        patch("apps.notifications.services.ultramsg.UltraMsgClient") as cls,
    ):
        create.return_value = SimpleNamespace(code="created", reservation=stay)
        cls.return_value.__enter__.return_value = fake
        response = client.post(url, {"action": "create_hostaway_and_request_accounting_link"})
    assert response.url == reverse("notifications:booking_detail", args=[stay.pk])
    create.assert_called_once_with(draft_id=draft.pk)
    fake.send_text.assert_called_once()
    assert WhatsAppDelivery.objects.get(reservation=stay).status == "sent"
    assert not ReservationPaymentRequest.objects.exists()
    assert not PaymentAttempt.objects.exists()


def test_default_new_manual_booking_still_queues_latest_branded_guest_message(settings):
    from apps.notifications.models import WhatsAppDelivery
    from apps.payments.payment_link_tasks import PAYMENT_MESSAGE_BRAND, payment_message

    stay, client = manual_stay(), Client()
    draft = ManualBookingDraft.objects.get(quote=stay.booking_intent.quote)
    draft.status = "ready_for_payment"
    draft.save(update_fields=["status"])
    client.force_login(owner())
    fake = accounting_sender(settings)
    with (
        patch("apps.reservations.operations_views.create_manual_booking_in_hostaway") as create,
        patch("apps.notifications.services.ultramsg.UltraMsgClient") as cls,
    ):
        create.return_value = SimpleNamespace(code="created", reservation=stay)
        cls.return_value.__enter__.return_value = fake
        response = client.post(
            reverse("notifications:manual_booking_detail", args=[draft.pk]),
            {"action": "create_hostaway_and_request_payment"},
        )
    assert response.url == reverse("notifications:booking_detail", args=[stay.pk])
    fake.send_text.assert_not_called()
    request = ReservationPaymentRequest.objects.get(reservation=stay)
    assert request.deliveries.get().kind == "link"
    assert PAYMENT_MESSAGE_BRAND in payment_message(request)
    assert "✨" in payment_message(request) and "🔗" in payment_message(request)
    assert not WhatsAppDelivery.objects.exists()


def test_default_guest_link_failure_never_falls_back_to_accountant():
    from apps.notifications.models import WhatsAppDelivery

    stay, client = manual_stay(), Client()
    client.force_login(owner())
    with (
        patch("apps.payments.payment_links.issue_request", side_effect=HyperPayCheckoutError("no")),
        patch("apps.reservations.operations_views.send_manual_payment_link_request") as accountant,
    ):
        response = client.post(
            reverse("notifications:booking_detail", args=[stay.pk]),
            {"action": "send_guest_payment_link"},
        )
    assert response.status_code == 302
    accountant.assert_not_called()
    assert not WhatsAppDelivery.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_postgres_parallel_accounting_and_site_link_select_only_one_collection_route(settings):
    from apps.notifications.models import WhatsAppDelivery
    from apps.notifications.services.ultramsg import send_manual_payment_link_request

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock test; executed by the PostgreSQL CI job")
    stay = manual_stay()
    accounting_sender(settings)
    barrier = Barrier(2)

    def select_route(accounting):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if accounting:
                send_manual_payment_link_request(reservation_id=stay.pk, owner_optional=True)
                return "accounting"
            issue_request(stay.pk)
            return "site"
        except HyperPayCheckoutError:
            return "blocked"
        finally:
            close_old_connections()

    with patch("apps.notifications.services.ultramsg._send_delivery"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result(timeout=20)
                for f in [pool.submit(select_route, True), pool.submit(select_route, False)]
            ]
    assert results.count("blocked") == 1
    assert (
        ReservationPaymentRequest.objects.filter(reservation=stay, status="open").count()
        + WhatsAppDelivery.objects.filter(reservation=stay).count()
    ) == 1
    assert not PaymentAttempt.objects.exists()


@pytest.mark.parametrize("previous_status", ["sent", "unknown", "failed", "queued", "sending"])
def test_operator_can_resend_immediately_after_any_delivery_status(previous_status):
    stay = manual_stay()
    request = issue_request(stay.pk)
    token = link_token(request)
    first = queue_delivery(request.pk)
    first.status = previous_status
    first.save(update_fields=["status"])
    second = queue_delivery(request.pk, resend=True)
    assert second.pk != first.pk and second.sequence == 2 and second.status == "queued"
    assert queue_delivery(request.pk).pk == second.pk
    assert issue_request(stay.pk).pk == request.pk
    assert link_token(request) == token
    assert ReservationPaymentRequest.objects.count() == 1
    assert not PaymentAttempt.objects.exists()


def test_admin_send_and_resend_each_request_new_delivery_even_from_old_page(settings):
    settings.ULTRAMSG_ENABLED = True
    stay, client = manual_stay(), Client()
    client.force_login(owner())
    url = reverse("notifications:booking_detail", args=[stay.pk])
    html = client.get(url).content.decode()
    assert 'value="send_guest_payment_link"' in html
    for action in [
        "send_guest_payment_link",
        "send_guest_payment_link",
        "resend_guest_payment_link",
    ]:
        response = client.post(url, {"action": action})
        assert response.status_code == 302 and response.url == url
    request = stay.payment_requests.get()
    assert list(request.deliveries.order_by("sequence").values_list("sequence", flat=True)) == [
        1,
        2,
        3,
    ]
    html = client.get(url).content.decode()
    assert 'value="resend_guest_payment_link"' in html
    assert 'value="send_guest_payment_link"' not in html
    assert "قد توصل أكثر من نسخة" in html
    assert ReservationPaymentRequest.objects.count() == 1
    assert not PaymentAttempt.objects.exists()
    fake = Mock()
    fake.send_text.return_value = {"sent": "true", "id": "test-message-repeat"}
    with patch("apps.payments.payment_link_tasks.UltraMsgClient") as cls:
        cls.return_value.__enter__.return_value = fake
        for delivery in request.deliveries.all():
            assert deliver_payment_request_task.run(delivery.pk)["status"] == "sent"
            assert deliver_payment_request_task.run(delivery.pk)["status"] == "sent"
    assert fake.send_text.call_count == 3
    assert len({call.kwargs["reference_id"] for call in fake.send_text.call_args_list}) == 3
    assert len({call.kwargs["body"] for call in fake.send_text.call_args_list}) == 1


@pytest.mark.parametrize("status", ["paid", "revoked", "review"])
def test_manual_resend_never_bypasses_closed_payment_request(status):
    request = issue_request(manual_stay().pk)
    first = queue_delivery(request.pk)
    request.status = status
    request.save(update_fields=["status"])
    with pytest.raises(HyperPayCheckoutError):
        queue_delivery(request.pk, resend=True)
    assert request.deliveries.count() == 1 and request.deliveries.get().pk == first.pk


def test_receipt_delivery_remains_single_even_when_resend_is_requested():
    request = issue_request(manual_stay().pk)
    request.status = "paid"
    request.save(update_fields=["status"])
    first = queue_delivery(request.pk, kind="receipt")
    assert queue_delivery(request.pk, kind="receipt", resend=True).pk == first.pk
    assert request.deliveries.count() == 1


def test_resend_during_pending_payment_reuses_link_without_creating_checkout():
    request = issue_request(manual_stay().pk)
    stub = HyperPayStub()
    attempt = checkout(request, stub=stub).attempt
    original_token = link_token(request)
    first = queue_delivery(request.pk)
    second = queue_delivery(request.pk, resend=True)
    assert first.pk != second.pk
    assert link_token(request) == original_token
    assert stub.checkout_calls == 1
    assert list(request.attempts.values_list("pk", flat=True)) == [attempt.pk]
    assert ReservationPaymentRequest.objects.count() == 1


def test_general_checkout_refuses_existing_manual_booking():
    stay = manual_stay()
    with pytest.raises(HyperPayCheckoutError, match="requires_payment_link"):
        HyperPayService(client=HyperPayStub()).create_checkout(stay.booking_intent)


def test_recovery_finishes_success_persisted_before_a_process_crash():
    request = issue_request(manual_stay().pk)
    attempt = checkout(request).attempt
    attempt.status, attempt.verified_at = "succeeded", timezone.now()
    attempt.provider_payment_id = "verified-before-crash"
    attempt.save()
    stub = HyperPayStub()
    with patch(
        "apps.payments.payment_links.HyperPayService", return_value=HyperPayService(client=stub)
    ):
        assert reconcile_requests()["checked"] == 1
    request.refresh_from_db()
    assert request.status == "paid"
    assert stub.payment_calls == 0


@pytest.mark.parametrize("currency", ["SAR", "MAD"])
def test_hostaway_receipt_uses_original_currency_and_is_idempotent(currency, settings):
    from apps.payments.hostaway_ledger import record_payment_receipt_task
    from apps.payments.models import HostawayFinancialEntry

    stay = manual_stay(currency=currency)
    stay.is_test = False
    stay.save()
    settings.HYPERPAY_ENVIRONMENT = "production"
    request = issue_request(stay.pk)
    attempt = checkout(request).attempt
    stub = HyperPayStub()
    stub.payment_document = approved_document(attempt)
    HyperPayService(client=stub).verify(attempt)
    attempt.refresh_from_db()
    entry = HostawayFinancialEntry.objects.create(payment_attempt=attempt)
    settings.HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED = True
    settings.HOSTAWAY_FINANCIAL_RECEIPTS_START_AT = "2020-01-01T00:00:00+00:00"
    settings.HYPERPAY_ENVIRONMENT = "production"
    fake = remote(stay)
    fake.create_offline_paid_charge.return_value = {"id": 987}
    with patch("apps.payments.hostaway_ledger.HostawayClient") as cls:
        cls.return_value.__enter__.return_value = fake
        assert record_payment_receipt_task.run(entry.pk)["status"] == "succeeded"
        assert record_payment_receipt_task.run(entry.pk)["status"] == "already_recorded"
    fake.create_offline_paid_charge.assert_called_once()
    assert fake.create_offline_paid_charge.call_args.kwargs["currency"] == currency
    assert fake.create_offline_paid_charge.call_args.kwargs["amount"] == request.source_amount
    if currency == "MAD":
        assert request.amount_sar != request.source_amount


def test_missing_or_stale_guest_acceptance_is_rejected_before_gateway():
    request = issue_request(manual_stay().pk)
    with pytest.raises(HyperPayCheckoutError, match="guest_acceptance_required"):
        create_request_checkout(request.pk, billing_data=billing(), acceptance={})
    assert PaymentAttempt.objects.count() == 0


def test_receipt_delivery_queued_once_after_commit(django_capture_on_commit_callbacks):
    request = issue_request(manual_stay().pk)
    attempt = checkout(request).attempt
    stub = HyperPayStub()
    stub.payment_document = approved_document(attempt)
    with patch("apps.payments.payment_link_tasks._dispatch"):
        with django_capture_on_commit_callbacks(execute=True):
            HyperPayService(client=stub).verify(attempt)
        with django_capture_on_commit_callbacks(execute=True):
            HyperPayService(client=stub).verify(attempt)
    assert request.deliveries.filter(kind="receipt").count() == 1


def test_environment_switch_does_not_poll_or_send_test_requests(settings):
    request = issue_request(manual_stay().pk)
    checkout(request)
    delivery = queue_delivery(request.pk)
    settings.HYPERPAY_ENVIRONMENT = "production"
    with patch("apps.payments.payment_links.HyperPayService") as gateway:
        assert reconcile_requests() == {"checked": 0}
        gateway.assert_not_called()
    with pytest.raises(ValueError, match="environment_mismatch"):
        queue_delivery(request.pk)
    with patch("apps.payments.payment_link_tasks.UltraMsgClient") as messaging:
        assert deliver_payment_request_task.run(delivery.pk)["status"] == "failed"
        messaging.assert_not_called()
    assert b'name="billing_city"' not in Client().get(page_url(request)).content


@pytest.mark.parametrize("status", ["refunded", "partially_refunded"])
def test_return_never_overwrites_a_refund_with_checkout_success(status):
    request = issue_request(manual_stay().pk)
    attempt = checkout(request).attempt
    attempt.status = status
    attempt.save()
    stub = HyperPayStub()
    HyperPayService(client=stub).verify(attempt)
    attempt.refresh_from_db()
    assert attempt.status == status
    assert stub.payment_calls == 0


def test_malformed_result_is_review_not_server_error():
    request = issue_request(manual_stay().pk)
    attempt = checkout(request).attempt
    stub = HyperPayStub()
    stub.payment_document = {"result": None}
    HyperPayService(client=stub).verify(attempt)
    attempt.refresh_from_db()
    assert attempt.status == "review"


@pytest.mark.django_db(transaction=True)
def test_postgres_parallel_manual_resends_keep_distinct_auditable_sequences():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock test; executed by the PostgreSQL CI job")
    request = issue_request(manual_stay().pk)
    barrier = Barrier(2)

    def resend():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return queue_delivery(request.pk, resend=True).sequence
        finally:
            close_old_connections()

    with patch("apps.payments.payment_link_tasks._dispatch"):
        queue_delivery(request.pk)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(resend), pool.submit(resend)]
            assert sorted(future.result(timeout=20) for future in futures) == [2, 3]
    assert request.deliveries.count() == 3
    assert ReservationPaymentRequest.objects.count() == 1
    assert not PaymentAttempt.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_issue_returns_one_open_request():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock test; executed by the PostgreSQL CI job")
    stay = manual_stay()
    barrier = Barrier(2)

    def issue():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return issue_request(stay.pk).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=20) for future in [pool.submit(issue), pool.submit(issue)]]
    assert results[0] == results[1]
    assert stay.payment_requests.filter(status="open").count() == 1


@pytest.mark.django_db(transaction=True)
def test_postgres_parallel_checkout_never_creates_second_gateway_session():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock test; executed by the PostgreSQL CI job")
    request = issue_request(manual_stay().pk)
    entered, release = Event(), Event()

    class PausedGateway(HyperPayStub):
        def create_checkout(self, payload):
            entered.set()
            assert release.wait(timeout=15)
            return super().create_checkout(payload)

    def start():
        close_old_connections()
        try:
            return checkout(request, stub=PausedGateway())
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(start)
        try:
            assert entered.wait(timeout=10)
            second_gateway = HyperPayStub()
            with pytest.raises(HyperPayCheckoutError, match="previous_payment_unresolved"):
                checkout(request, stub=second_gateway)
            assert second_gateway.checkout_calls == 0
            assert PaymentAttempt.objects.filter(payment_request=request).count() == 1
        finally:
            release.set()
        first.result(timeout=20)


@pytest.mark.parametrize("zone", ["Asia/Riyadh", "Africa/Casablanca", "Europe/Paris"])
def test_same_day_link_uses_actual_property_arrival_not_midnight_intent_expiry(zone, monkeypatch):
    from apps.reservations.services.host_policy import check_in_datetime

    stay = manual_stay()
    stay.property.time_zone_name = zone
    stay.property.check_in_time_start = 15
    stay.property.save()
    arrival = check_in_datetime(stay.property, stay.check_in)
    clock = [arrival - timedelta(seconds=1)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    request = issue_request(stay.pk)
    assert request.expires_at == arrival
    assert request.expires_at > stay.booking_intent.expires_at
    clock[0] = arrival
    with pytest.raises(HyperPayCheckoutError, match="stay_already_started"):
        issue_request(stay.pk)
