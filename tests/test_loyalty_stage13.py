"""Offline rollout/rollback checks; no live activation or provider requests."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from apps.loyalty.checkout import cash_due
from apps.loyalty.models import Ledger
from apps.loyalty.services import available, hold, reconcile, release
from apps.payments.hyperpay.exceptions import HyperPayCheckoutError
from apps.payments.hyperpay.service import HyperPayService
from apps.payments.models import PaymentAttempt
from apps.reservations.services.hostaway_booking import prepare_local_reservation
from tests.test_hyperpay import HYPERPAY_SETTINGS, HyperPayStub, payable_intent
from tests.test_loyalty_stage8 import booking, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def local_program(settings, monkeypatch):
    settings.LOYALTY_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 11, 13, tzinfo=UTC))


def held_checkout():
    guest = user("rollout")
    member = join(guest)
    reconcile(booking(guest).pk)
    intent = payable_intent()
    intent.customer = guest
    intent.save(update_fields=["customer"])
    reservation = prepare_local_reservation(intent)
    hold(guest, "TEST-ROLLOUT-HOLD", reservation.pk, "20.00")
    reservation.refresh_from_db()
    return guest, member, intent, reservation


@pytest.mark.parametrize("freeze", [False, True])
def test_disabling_program_does_not_turn_held_points_into_cash(freeze):
    _, member, intent, reservation = held_checkout()
    original_state = dict(reservation.loyalty_state)
    original_ledger = list(Ledger.objects.values())
    original_available = available(member)
    with override_settings(LOYALTY_ENABLED=False):
        with pytest.raises(ValidationError, match="متوقف"):
            cash_due(intent, freeze=freeze)
    reservation.refresh_from_db()
    assert reservation.loyalty_state == original_state
    assert list(Ledger.objects.values()) == original_ledger
    assert available(member) == original_available
    assert not PaymentAttempt.objects.filter(booking_intent=intent).exists()


@pytest.mark.parametrize(
    "state",
    [
        {"redemption": {"value": "20.00", "restored": "0.00"}},
        {"checkout_frozen": {"cash": "1230.00", "points": "20.00"}},
        {"points_confirmation_started": True},
    ],
)
def test_rollback_blocks_point_settlements_even_without_a_hold(state):
    intent = payable_intent()
    reservation = prepare_local_reservation(intent)
    reservation.loyalty_state = state
    reservation.save(update_fields=["loyalty_state"])
    with override_settings(LOYALTY_ENABLED=False):
        with pytest.raises(ValidationError, match="متوقف"):
            cash_due(intent)
    reservation.refresh_from_db()
    assert reservation.loyalty_state == state


@pytest.mark.parametrize("existing_session", [False, True])
def test_rollback_refuses_new_or_reused_checkout_before_calling_gateway(existing_session):
    _, _, intent, reservation = held_checkout()
    stub = HyperPayStub()
    with override_settings(**HYPERPAY_SETTINGS):
        if existing_session:
            HyperPayService(client=stub).create_checkout(intent)
        calls = stub.checkout_calls
        receipts = list(PaymentAttempt.objects.filter(booking_intent=intent).values())
        reservation.refresh_from_db()
        state = dict(reservation.loyalty_state)
        with override_settings(LOYALTY_ENABLED=False):
            with pytest.raises(HyperPayCheckoutError, match="loyalty_payment_allocation_invalid"):
                HyperPayService(client=stub).create_checkout(intent)
    assert stub.checkout_calls == calls
    assert list(PaymentAttempt.objects.filter(booking_intent=intent).values()) == receipts
    reservation.refresh_from_db()
    assert reservation.loyalty_state == state


@pytest.mark.parametrize("reservation_state", [None, {}, {"arrival": {"reference": "TEST"}}])
def test_cash_only_bookings_continue_when_program_is_disabled(
    reservation_state, django_assert_num_queries
):
    intent = payable_intent()
    if reservation_state is not None:
        reservation = prepare_local_reservation(intent)
        reservation.loyalty_state = reservation_state
        reservation.save(update_fields=["loyalty_state"])
    with override_settings(LOYALTY_ENABLED=False), django_assert_num_queries(1):
        assert cash_due(intent, freeze=True) == Decimal("1250.00")
    assert not Ledger.objects.exists()


def test_reenable_preserves_original_allocation_without_duplicate_hold():
    _, member, intent, reservation = held_checkout()
    held_value = reservation.loyalty_state["hold"]["value"]
    with override_settings(LOYALTY_ENABLED=False):
        with pytest.raises(ValidationError):
            cash_due(intent)
    with override_settings(LOYALTY_ENABLED=True):
        assert cash_due(intent) == Decimal("1230.00")
        assert cash_due(intent, freeze=True) == Decimal("1230.00")
    reservation.refresh_from_db()
    assert reservation.loyalty_state["hold"]["value"] == held_value
    assert Decimal(held_value) == Decimal("20.00")
    assert reservation.loyalty_state["checkout_frozen"] == {
        "cash": "1230.00",
        "points": held_value,
    }
    assert available(member) == Decimal("2.50")
    assert Ledger.objects.filter(member=member).count() == 1


def test_safely_released_hold_can_continue_as_cash_only_during_rollback():
    guest, member, intent, reservation = held_checkout()
    cash_due(intent, freeze=True)
    # No checkout was submitted to the gateway. Use the existing verified release path.
    release(guest, "TEST-ROLLOUT-RELEASE", reservation.pk)
    with override_settings(LOYALTY_ENABLED=False):
        assert cash_due(intent) == Decimal("1250.00")
    reservation.refresh_from_db()
    assert "hold" not in reservation.loyalty_state
    assert "checkout_frozen" not in reservation.loyalty_state
    assert available(member) == Decimal("22.50")
