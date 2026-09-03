from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.payments.models import PaymentAttempt
from apps.reservations.models import (
    BookingIntent,
    BookingModificationRequest,
    HostawayModificationOperation,
    HostawayReservationOperation,
    Reservation,
)
from apps.reservations.services.booking import consume_revalidated_quote
from tests.test_booking_models_services import guest_data, make_availability
from tests.test_booking_views_admin import owned_client_quote

pytestmark = pytest.mark.django_db


def make_owned_intent() -> tuple[Client, BookingIntent]:
    client, quote, _reference = owned_client_quote()
    intent = consume_revalidated_quote(
        quote_id=quote.pk,
        session_hash=quote.session_key_hash,
        idempotency_key="sandbox-intent-000000000000000000",
        guest_data=guest_data(),
        revalidated=make_availability(quote.property),
    ).intent
    assert intent is not None
    return client, intent


def test_customer_can_register_sign_in_and_open_empty_dashboard() -> None:
    client = Client()
    response = client.post(
        "/register/",
        {
            "first_name": "Test",
            "last_name": "Guest",
            "email": "Customer@Example.invalid",
            "password1": "Correct-Horse-Battery-2026",
            "password2": "Correct-Horse-Battery-2026",
            "accept_terms": "on",
        },
    )
    assert response.status_code == 302
    assert response.url == "/my-bookings/"
    user = get_user_model().objects.get()
    assert user.email == "customer@example.invalid"
    dashboard = client.get("/my-bookings/")
    assert dashboard.status_code == 200


def test_account_fields_and_french_copy_are_customer_ready() -> None:
    client = Client()
    login_page = client.get("/login/", HTTP_ACCEPT_LANGUAGE="fr")
    register_page = client.get("/register/", HTTP_ACCEPT_LANGUAGE="fr")
    login_content = login_page.content.decode()
    register_content = register_page.content.decode()

    assert login_page.status_code == register_page.status_code == 200
    assert "Se connecter" in login_content
    assert "Toutes vos réservations au même endroit" in login_content
    assert 'autocomplete="email"' in login_content
    assert 'autocomplete="current-password"' in login_content
    assert "Créer un compte" in register_content
    assert 'autocomplete="given-name"' in register_content
    assert 'autocomplete="family-name"' in register_content
    assert register_content.count('autocomplete="new-password"') == 2
    assert 'data-close-label="Fermer le menu"' in register_content


def test_signed_in_guest_details_are_prefilled_with_account_identity() -> None:
    client, _quote, reference = owned_client_quote()
    user = get_user_model().objects.create_user(
        username="prefill@example.invalid",
        email="prefill@example.invalid",
        first_name="Layla",
        last_name="Guest",
        password="Correct-Horse-Battery-2026",
    )
    client.force_login(user)

    response = client.get(reverse("reservations:quote_detail", args=[reference]))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'value="Layla"' in content
    assert 'value="Guest"' in content
    assert 'value="prefill@example.invalid"' in content
    assert 'autocomplete="given-name"' in content
    assert 'inputmode="tel"' in content


def test_booking_management_is_available_from_linked_account() -> None:
    client, intent = make_owned_intent()
    user = get_user_model().objects.create_user(
        username=intent.guest_email,
        email=intent.guest_email,
        password="Correct-Horse-Battery-2026",
    )
    intent.customer = user
    intent.save(update_fields=["customer"])
    reservation = Reservation.objects.create(
        booking_intent=intent,
        property=intent.property,
        hostaway_listing_id=intent.property.hostaway_listing_id,
        source_type=Reservation.SourceType.DIRECT_WEBSITE,
        normalized_status=Reservation.Status.CONFIRMED,
        payment_status="sandbox_paid",
        is_test=True,
        check_in=intent.check_in,
        check_out=intent.check_out,
        nights=intent.nights,
        guests=intent.guests,
        currency=intent.currency,
        total_price=intent.total_price,
        confirmed_at=timezone.now(),
    )
    account_client = Client()
    assert account_client.login(username=user.username, password="Correct-Horse-Battery-2026")
    response = account_client.get(f"/reservations/manage/{reservation.public_reference}/")
    assert response.status_code == 200
    assert response.headers["Referrer-Policy"] == "same-origin"
    content = response.content.decode()
    assert 'data-iso-date=""' in content
    assert 'inputmode="numeric"' in content
    assert f'max="{reservation.property.person_capacity}"' in content
    date_widget = response.context["date_form"].fields["new_check_in"].widget
    assert date_widget.input_type == "date"
    assert "data-management-date-input" in date_widget.attrs
    assert 'data-management-date-trigger="new_check_in"' in content
    assert 'data-calendar-kind="range"' in content


@override_settings(PAYMENT_SANDBOX_ENABLED=True)
def test_sandbox_booking_success_is_local_idempotent_and_never_calls_hostaway() -> None:
    client, intent = make_owned_intent()
    url = f"/payments/sandbox/booking/{intent.public_reference}/"
    assert client.get(url).status_code == 200

    first = client.post(url, {"action": "success"})
    second = client.post(url, {"action": "success"})

    assert first.status_code == second.status_code == 302
    reservation = Reservation.objects.get(booking_intent=intent)
    assert reservation.is_test is True
    assert reservation.normalized_status == Reservation.Status.CONFIRMED
    assert reservation.hostaway_reservation_id is None
    assert PaymentAttempt.objects.filter(status=PaymentAttempt.Status.SUCCEEDED).count() == 1
    assert HostawayReservationOperation.objects.count() == 0
    assert HostawayModificationOperation.objects.count() == 0


@override_settings(PAYMENT_SANDBOX_ENABLED=True)
def test_sandbox_difference_payment_updates_only_local_request() -> None:
    client, intent = make_owned_intent()
    booking_url = f"/payments/sandbox/booking/{intent.public_reference}/"
    client.post(booking_url, {"action": "success"})
    reservation = Reservation.objects.get(booking_intent=intent)
    modification = BookingModificationRequest.objects.create(
        reservation=reservation,
        request_type=BookingModificationRequest.RequestType.EXTEND_STAY,
        status=BookingModificationRequest.Status.AWAITING_PAYMENT,
        old_check_in=reservation.check_in,
        old_check_out=reservation.check_out,
        new_check_in=reservation.check_in,
        new_check_out=reservation.check_out + timedelta(days=1),
        old_guests=reservation.guests,
        new_guests=reservation.guests,
        old_total=reservation.total_price,
        new_total=reservation.total_price + Decimal("250"),
        price_difference=Decimal("250"),
        currency=reservation.currency,
        idempotency_key="sandbox-modification-000000000000000",
        session_key_hash=intent.session_key_hash,
        expires_at=timezone.now() + timedelta(minutes=30),
    )
    url = f"/payments/sandbox/modification/{modification.public_reference}/"

    response = client.post(url, {"action": "success"})

    assert response.status_code == 302
    modification.refresh_from_db()
    reservation.refresh_from_db()
    assert modification.status == BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
    assert reservation.check_out == modification.old_check_out
    assert reservation.total_price == modification.old_total
    attempt = PaymentAttempt.objects.get(modification_request=modification)
    assert attempt.amount == Decimal("250")
    assert attempt.status == PaymentAttempt.Status.SUCCEEDED
    assert HostawayReservationOperation.objects.count() == 0
    assert HostawayModificationOperation.objects.count() == 0


def test_sandbox_routes_are_hidden_when_disabled() -> None:
    client, intent = make_owned_intent()
    response = client.get(f"/payments/sandbox/booking/{intent.public_reference}/")
    assert response.status_code == 404


def test_root_service_worker_is_present_and_never_cached() -> None:
    response = Client().get("/service-worker.js")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/javascript")
    assert response.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
    assert response.headers["Service-Worker-Allowed"] == "/"
