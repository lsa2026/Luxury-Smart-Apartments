"""Real website entrypoints, isolated guests and provider doubles; strictly offline."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.loyalty.checkout import cash_due, confirm_points_booking
from apps.loyalty.models import Ledger
from apps.loyalty.services import available, balance, hold, reconcile
from apps.payments.currency import CurrencyService
from apps.payments.hyperpay.service import HyperPayService
from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingIntent, Reservation
from apps.reservations.services.hostaway_booking import (
    HostawayBookingService,
    prepare_local_reservation,
)
from tests.test_hostaway_booking_phase5 import (
    AvailabilityStub,
    CreateClientStub,
    complete_availability,
    create_result,
)
from tests.test_hyperpay import HYPERPAY_SETTINGS, HyperPayStub, payable_intent
from tests.test_loyalty_stage8 import booking, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def program(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_UI_PREVIEW = settings.DEBUG = True
    # PostgreSQL deliberately refuses SQLite-only draft preview consent.
    # Exercise the approved-terms path using fictional test references instead.
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "test-not-published-v1"
    settings.LOYALTY_TERMS_REFERENCE = "/test-only-loyalty-terms/"
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.HOSTAWAY_LIVE_BOOKING_ENABLED = True
    settings.HOSTAWAY_DIRECT_CHANNEL_ID = 2000
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    cache.clear()
    return clock


def checkout():
    guest = user("checkout")
    member = join(guest)
    reconcile(booking(guest, "50000.00").pk)
    intent = payable_intent()
    intent.customer = guest
    intent.save(update_fields=["customer"])
    client = Client()
    client.force_login(guest)
    return guest, member, intent, client


def points_url(intent):
    return reverse("reservations:booking_points", args=[intent.public_reference])


def test_expiration_releases_definitely_unpaid_hold_but_not_uncertain_card(program):
    from apps.loyalty.checkout import release_expired_points

    guest, member, intent, client = checkout()
    client.post(
        points_url(intent),
        {"action": "apply", "points": "100.00", "request_reference": "TEST-EXPIRED-HOLD-01"},
    )
    booking = Reservation.objects.get(booking_intent=intent)
    intent.status = BookingIntent.Status.EXPIRED
    intent.save()
    booking.loyalty_state["checkout_frozen"] = {"cash": "1150.00", "points": "100.00"}
    booking.save()
    attempt = PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="hyperpay",
        status=PaymentAttempt.Status.UNKNOWN,
        amount="1150.00",
        currency="SAR",
        idempotency_key="TEST-UNKNOWN-EXPIRY",
    )
    release_expired_points(intent.pk)
    assert available(member) == Decimal("1400.00")
    attempt.status = PaymentAttempt.Status.FAILED
    attempt.save()
    release_expired_points(intent.pk)
    assert available(member) == Decimal("1500.00")


def test_partial_points_visible_and_cash_collection_reduced_without_changing_quote():
    guest, member, intent, client = checkout()
    response = client.post(
        points_url(intent),
        {"action": "apply", "points": "250.00", "request_reference": "TEST-CHECKOUT-ALLOCATE-01"},
    )
    assert response.status_code == 302
    reservation = Reservation.objects.get(booking_intent=intent)
    assert cash_due(intent) == Decimal("1000.00")
    assert available(member) == Decimal("1250.00") and balance(member) == Decimal("1500.00")
    intent.refresh_from_db()
    assert intent.total_price == Decimal("1250.00")
    page = client.get(reverse("reservations:intent_detail", args=[intent.public_reference]))
    assert page.context["points_checkout"]["cash"] == 1000
    assert "no-store" in page["Cache-Control"]
    with override_settings(**HYPERPAY_SETTINGS):
        stub = HyperPayStub()
        session = HyperPayService(client=stub).create_checkout(intent)
    assert session.attempt.amount == 1000 and stub.payload["amount"] == "1000.00"
    assert not Ledger.objects.filter(reservation=reservation).exists()


@pytest.mark.parametrize("input_points", ["25", "٢٥"])
def test_guest_example_2571_balance_uses_25_on_1100_booking_without_spending(input_points):
    guest = user("guest-25-points")
    member = join(guest)
    reconcile(booking(guest, "857.00").pk)
    assert balance(member) == Decimal("25.71")
    intent = payable_intent()
    intent.customer = guest
    intent.total_price = Decimal("1100.00")
    with CurrencyService() as currency:
        snapshot = currency.create_quote(
            source_amount=intent.total_price,
            source_currency="SAR",
            display_currency="SAR",
            quote_created_at=intent.created_at,
            quote_expires_at=intent.expires_at,
        )
    intent.payment_amount_sar = snapshot.payment_amount_sar
    intent.exchange_rate_snapshot = dict(snapshot.snapshot)
    intent.save()
    client = Client()
    client.force_login(guest)
    page = client.get(reverse("reservations:intent_detail", args=[intent.public_reference]))
    reference = page.context["points_checkout"]["request_reference"]
    response = client.post(
        points_url(intent),
        {"action": "apply", "points": input_points, "request_reference": reference},
        follow=True,
    )
    assert Decimal(response.context["points_checkout"]["points"]) == Decimal("25.00")
    assert response.context["points_checkout"]["cash"] == Decimal("1075.00")
    assert available(member) == Decimal("0.71")
    assert balance(member) == Decimal("25.71")
    reservation = Reservation.objects.get(booking_intent=intent)
    assert not Ledger.objects.filter(reservation=reservation).exists()
    with override_settings(**HYPERPAY_SETTINGS):
        stub = HyperPayStub()
        session = HyperPayService(client=stub).create_checkout(intent)
    assert session.attempt.amount == Decimal("1075.00")
    assert stub.payload["amount"] == "1075.00"
    session.attempt.status = PaymentAttempt.Status.FAILED
    session.attempt.save(update_fields=["status"])
    response = client.post(
        points_url(intent),
        {"action": "remove", "request_reference": "TEST-REMOVE-25-POINTS-01"},
        follow=True,
    )
    assert available(member) == Decimal("25.71")
    assert response.context["points_checkout"]["cash"] == Decimal("1100.00")


def test_duplicate_apply_is_same_hold_not_a_second_reservation():
    _, member, intent, client = checkout()
    data = {"action": "apply", "points": "25.00", "request_reference": "TEST-CHECKOUT-REPEAT-01"}
    client.post(points_url(intent), data)
    client.post(points_url(intent), data)
    assert Reservation.objects.filter(booking_intent=intent).count() == 1
    assert available(member) == Decimal("1475.00")


@pytest.mark.parametrize("flag", ["LOYALTY_ENABLED", "LOYALTY_UI_ENABLED"])
def test_disabled_program_preserves_cash_journey_and_denies_points(flag, settings):
    _, _, intent, client = checkout()
    setattr(settings, flag, False)
    assert client.post(points_url(intent), {"action": "apply"}).status_code == 403
    assert not Reservation.objects.filter(booking_intent=intent).exists()
    page = client.get(reverse("reservations:intent_detail", args=[intent.public_reference]))
    assert "points_checkout" not in page.context and cash_due(intent) == intent.total_price


def test_other_guest_cannot_select_points_on_owned_booking():
    _, _, intent, _ = checkout()
    other = user("other-checkout")
    client = Client()
    client.force_login(other)
    assert client.post(points_url(intent), {"action": "apply"}).status_code == 404
    assert not Reservation.objects.filter(booking_intent=intent).exists()


@pytest.mark.parametrize("value", ["1500.01", "-1", "0", "NaN", "12.001"])
def test_invalid_or_excess_points_do_not_hold(value):
    _, member, intent, client = checkout()
    client.post(
        points_url(intent),
        {"action": "apply", "points": value, "request_reference": "TEST-CHECKOUT-INVALID-01"},
    )
    reservation = Reservation.objects.get(booking_intent=intent)
    assert not reservation.loyalty_state.get("hold") and available(member) == 1500


def test_hold_cannot_be_added_after_card_checkout_started():
    guest, _, intent, _ = checkout()
    reservation = prepare_local_reservation(intent)
    PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="hyperpay",
        amount=1250,
        currency="SAR",
        status="pending",
        idempotency_key="TEST-PENDING-CHECKOUT",
    )
    with pytest.raises(ValidationError):
        hold(guest, "TEST-LATE-HOLD-01", reservation.pk, "10.00")


def test_full_points_uses_real_inventory_safeguards_without_payment_attempt():
    guest, member, intent, client = checkout()
    client.post(
        points_url(intent),
        {"action": "apply", "points": "1250.00", "request_reference": "TEST-FULL-POINTS-01"},
    )
    reservation = Reservation.objects.get(booking_intent=intent)
    stub = CreateClientStub(create_result(reservation, payment_status="unpaid"))
    service = HostawayBookingService(
        client=stub, availability_service=AvailabilityStub(complete_availability(intent))
    )
    result = confirm_points_booking(guest, reservation.pk, booking_service=service)
    assert result["status"] == "confirmed" and stub.calls == 1
    reservation.refresh_from_db()
    intent.refresh_from_db()
    assert reservation.payment_status == "paid_with_points"
    assert intent.status == BookingIntent.Status.COMPLETED
    assert balance(member) == 250
    assert not PaymentAttempt.objects.filter(booking_intent=intent).exists()
    assert Ledger.objects.filter(reservation=reservation, event_type="redeem").count() == 1


def test_full_points_never_uses_manual_price_override():
    guest, member, intent, client = checkout()
    client.post(
        points_url(intent),
        {"action": "apply", "points": "1250.00", "request_reference": "TEST-PRICE-CHANGED-01"},
    )
    reservation = Reservation.objects.get(booking_intent=intent)
    stub = CreateClientStub()
    service = HostawayBookingService(
        client=stub,
        availability_service=AvailabilityStub(
            complete_availability(intent, total=Decimal("1300.00"))
        ),
    )
    result = confirm_points_booking(guest, reservation.pk, booking_service=service)
    assert result["status"] == "revalidated_price_changed" and stub.calls == 0
    reservation.refresh_from_db()
    assert not reservation.loyalty_state.get("hold") and available(member) == 1500


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_checkout_copy_and_currency_eligibility(language, monkeypatch):
    _, _, intent, client = checkout()
    intent.language = language
    intent.save(update_fields=["language"])
    page = client.get(reverse("reservations:intent_detail", args=[intent.public_reference]))
    from apps.loyalty.booking_ui import TEXT

    assert TEXT[language]["title"] in page.content.decode()
    intent.currency = "MAD"
    intent.save(update_fields=["currency"])
    from apps.payments.currency import CurrencyService, ExchangeRateUnavailableError

    def offline_rates(*args, **kwargs):
        raise ExchangeRateUnavailableError("isolated-test")

    monkeypatch.setattr(CurrencyService, "get_rates", offline_rates)
    page = client.get(reverse("reservations:intent_detail", args=[intent.public_reference]))
    assert "points_checkout" not in page.context


def test_csrf_required_before_placing_hold():
    guest, _, intent, _ = checkout()
    client = Client(enforce_csrf_checks=True)
    client.force_login(guest)
    assert client.post(points_url(intent), {"action": "apply"}).status_code == 403
    assert not Reservation.objects.filter(booking_intent=intent).exists()
