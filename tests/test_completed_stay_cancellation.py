"""Completed stays cannot be cancelled through fresh or stale service requests."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.utils import timezone

from apps.payments.models import PaymentAttempt
from apps.properties.models import Property
from apps.reservations.models import (
    BookingIntent,
    BookingModificationRequest,
    HostawayModificationOperation,
    RefundObligation,
    Reservation,
)
from apps.reservations.services import hostaway_modifications
from apps.reservations.services.automatic_modifications import execute_automatic_modification
from apps.reservations.services.hostaway_modifications import HostawayModificationService
from apps.reservations.services.modifications import ModificationService
from apps.reservations.services.stay_lifecycle import (
    STAY_COMPLETED,
    cancellation_blocker,
    checkout_datetime,
)
from tests.test_booking_modifications_phase6 import confirmed_reservation

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def enable_cancellation(settings):
    settings.BOOKING_CANCELLATION_REQUEST_ENABLED = True
    settings.BOOKING_AUTOMATIC_CANCELLATION_ENABLED = True
    settings.BOOKING_AUTOMATIC_REFUND_ENABLED = True
    settings.HOSTAWAY_LIVE_CANCELLATION_ENABLED = True


def local_booking(**property_fields):
    return Reservation(
        property=Property(**property_fields),
        check_out=date(2026, 10, 11),
        normalized_status=Reservation.Status.CONFIRMED,
        source_type=Reservation.SourceType.DIRECT_WEBSITE,
        hostaway_reservation_id=77001,
    )


@pytest.mark.parametrize(
    ("display", "imported", "expected"),
    [(14, 11, 14), (None, 11, 11), (None, None, 12), (0, 11, 0), (25, 11, 11), (True, 25, 12)],
)
def test_checkout_hour_precedence_and_validation(display, imported, expected):
    booking = local_booking(display_check_out_hour=display, check_out_time=imported)
    assert checkout_datetime(booking) == datetime(
        2026, 10, 11, expected, tzinfo=ZoneInfo("Asia/Riyadh")
    )


def test_hour_24_means_following_midnight():
    booking = local_booking(display_check_out_hour=24)
    assert checkout_datetime(booking) == datetime(2026, 10, 12, tzinfo=ZoneInfo("Asia/Riyadh"))


@pytest.mark.parametrize(
    ("country", "listing_zone", "expected_zone"),
    [
        ("SA", "", "Asia/Riyadh"),
        ("ma", "", "Africa/Casablanca"),
        ("MA", "invalid/timezone", "Africa/Casablanca"),
        ("SA", "invalid/timezone", "Asia/Riyadh"),
        ("MA", "Asia/Riyadh", "Asia/Riyadh"),
    ],
)
def test_property_timezone_does_not_depend_on_viewer(country, listing_zone, expected_zone):
    booking = local_booking(country_code=country, time_zone_name=listing_zone)
    with timezone.override("America/Los_Angeles"):
        assert checkout_datetime(booking).tzinfo.key == expected_zone


@pytest.mark.parametrize(("day", "utc_hour"), [(date(2025, 3, 1), 12), (date(2025, 10, 11), 11)])
def test_casablanca_checkout_uses_offset_on_the_stay_date(day, utc_hour):
    booking = local_booking(country_code="MA")
    booking.check_out = day
    assert checkout_datetime(booking).astimezone(UTC) == datetime(
        day.year, day.month, day.day, utc_hour, tzinfo=UTC
    )


@pytest.mark.parametrize("seconds", [-1, 0, 1])
def test_checkout_boundary_is_inclusive(seconds):
    booking = local_booking(time_zone_name="Asia/Riyadh")
    moment = checkout_datetime(booking).astimezone(UTC) + timedelta(seconds=seconds)
    assert cancellation_blocker(booking, now=moment) == ("" if seconds < 0 else STAY_COMPLETED)


@pytest.mark.parametrize("field", ["normalized_status", "hostaway_status"])
def test_explicit_completion_blocks_even_before_checkout(field):
    booking = local_booking()
    setattr(booking, field, "completed")
    assert cancellation_blocker(booking, now=checkout_datetime(booking) - timedelta(days=1))


def test_paid_and_completed_booking_intent_do_not_mean_stay_completed():
    booking = local_booking()
    booking.booking_intent = BookingIntent(status=BookingIntent.Status.COMPLETED)
    booking.payment_status = "paid"
    assert cancellation_blocker(booking, now=checkout_datetime(booking) - timedelta(days=1)) == ""


@pytest.mark.parametrize(
    "status", [Reservation.Status.READY_FOR_HOSTAWAY, Reservation.Status.CREATE_FAILED]
)
def test_orphaned_unconfirmed_records_are_not_completed_stays(status):
    booking = local_booking()
    booking.normalized_status = status
    booking.hostaway_reservation_id = None
    assert cancellation_blocker(booking, now=checkout_datetime(booking) + timedelta(days=1)) == ""


def pending_cancellation(
    reservation, *, status=BookingModificationRequest.Status.READY_FOR_HOSTAWAY
):
    return BookingModificationRequest.objects.create(
        reservation=reservation,
        request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION,
        status=status,
        old_check_in=reservation.check_in,
        old_check_out=reservation.check_out,
        old_guests=reservation.guests,
        old_total=reservation.total_price,
        price_difference=Decimal("0"),
        currency=reservation.currency,
        idempotency_key="completed-stay-cancellation-test-" + "x" * 32,
        session_key_hash=reservation.booking_intent.session_key_hash,
        expires_at=timezone.now() + timedelta(days=1),
    )


def move_checkout_into_past(reservation):
    end = timezone.localdate() - timedelta(days=1)
    Reservation.objects.filter(pk=reservation.pk).update(
        check_in=end - timedelta(days=2), check_out=end, nights=2
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("owner_override", [False, True])
def test_creation_reloads_past_checkout_from_database_under_lock(owner_override, monkeypatch):
    reservation = confirmed_reservation()
    move_checkout_into_past(reservation)
    from apps.reservations.services import modifications

    original = modifications.cancellation_blocker

    def guarded(booking):
        assert connection.in_atomic_block
        return original(booking)

    monkeypatch.setattr(modifications, "cancellation_blocker", guarded)
    with ModificationService() as service:
        outcome = service.create_cancellation_request(
            reservation,
            session_hash=reservation.booking_intent.session_key_hash,
            owner_override=owner_override,
        )
    assert outcome.code == STAY_COMPLETED
    assert not BookingModificationRequest.objects.exists()
    assert not RefundObligation.objects.exists()


@pytest.mark.parametrize("owner_override", [False, True])
def test_existing_request_cannot_be_returned_after_checkout(owner_override, monkeypatch):
    reservation = confirmed_reservation()
    moment = checkout_datetime(reservation)
    monkeypatch.setattr(timezone, "now", lambda: moment - timedelta(seconds=1))
    with ModificationService() as service:
        first = service.create_cancellation_request(
            reservation,
            session_hash=reservation.booking_intent.session_key_hash,
            owner_override=owner_override,
        )
        assert first.code == "created"
        monkeypatch.setattr(timezone, "now", lambda: moment)
        second = service.create_cancellation_request(
            reservation,
            session_hash=reservation.booking_intent.session_key_hash,
            owner_override=owner_override,
        )
    assert second.code == STAY_COMPLETED
    assert second.request is None
    assert BookingModificationRequest.objects.count() == 1


@pytest.mark.parametrize("execution", ["automatic", "hostaway"])
@pytest.mark.parametrize("state", ["elapsed", "completed"])
def test_stale_ready_request_blocks_every_execution_without_side_effects(execution, state):
    reservation = confirmed_reservation()
    request = pending_cancellation(reservation)
    if state == "elapsed":
        move_checkout_into_past(reservation)
    else:
        Reservation.objects.filter(pk=reservation.pk).update(hostaway_status="completed")
    client, refunds = Mock(), Mock()
    service = HostawayModificationService(client=client)
    if execution == "automatic":
        outcome = execute_automatic_modification(request, service=service, refund_service=refunds)
    else:
        outcome = service.execute(request)
    assert outcome.code == STAY_COMPLETED
    assert client.mock_calls == refunds.mock_calls == []
    request.refresh_from_db()
    assert request.status == request.Status.READY_FOR_HOSTAWAY
    assert request.approved_at is None
    assert not HostawayModificationOperation.objects.exists()
    assert not RefundObligation.objects.exists()


def test_checkout_change_between_precheck_and_operation_is_rechecked_under_lock(monkeypatch):
    reservation = confirmed_reservation()
    request = pending_cancellation(reservation)
    client = Mock()
    prepare = hostaway_modifications._prepare_operation

    def changed_before_lock(modification):
        move_checkout_into_past(reservation)
        return prepare(modification)

    monkeypatch.setattr(hostaway_modifications, "_prepare_operation", changed_before_lock)
    outcome = HostawayModificationService(client=client).execute(request)
    assert outcome.code == STAY_COMPLETED
    assert client.mock_calls == []
    operation = HostawayModificationOperation.objects.get()
    assert operation.status == operation.Status.BLOCKED
    assert operation.attempt_count == 0
    assert operation.error_code == STAY_COMPLETED
    request.refresh_from_db()
    assert request.status == request.Status.READY_FOR_HOSTAWAY


@pytest.mark.django_db(transaction=True)
def test_clock_crossing_checkout_during_preparation_is_blocked_under_lock(monkeypatch):
    reservation = confirmed_reservation()
    request = pending_cancellation(reservation)
    moment = checkout_datetime(reservation)
    original = hostaway_modifications.cancellation_blocker
    calls = []

    def check_at_boundary(booking):
        calls.append(connection.in_atomic_block)
        now = moment - timedelta(seconds=1) if len(calls) == 1 else moment
        return original(booking, now=now)

    monkeypatch.setattr(hostaway_modifications, "cancellation_blocker", check_at_boundary)
    client = Mock()
    outcome = HostawayModificationService(client=client).execute(request)
    assert calls == [False, True]
    assert outcome.code == STAY_COMPLETED
    assert client.mock_calls == []
    assert outcome.operation.attempt_count == 0


@pytest.mark.django_db(transaction=True)
def test_old_pending_approval_is_blocked_under_automatic_execution_lock(monkeypatch):
    from apps.reservations.services import automatic_modifications

    reservation = confirmed_reservation()
    request = pending_cancellation(
        reservation, status=BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
    )
    move_checkout_into_past(reservation)
    original = automatic_modifications.cancellation_blocker

    def guarded(booking):
        assert connection.in_atomic_block
        return original(booking)

    monkeypatch.setattr(automatic_modifications, "cancellation_blocker", guarded)
    service, refunds = Mock(), Mock()
    outcome = execute_automatic_modification(request, service=service, refund_service=refunds)
    assert outcome.code == STAY_COMPLETED
    assert service.mock_calls == refunds.mock_calls == []
    request.refresh_from_db()
    assert request.status == request.Status.PENDING_ADMIN_APPROVAL
    assert request.approved_at is None
    assert not RefundObligation.objects.exists()
    assert not HostawayModificationOperation.objects.exists()


@pytest.mark.parametrize("execution", ["automatic", "hostaway"])
def test_completed_cancellation_keeps_idempotency_after_checkout(execution):
    reservation = confirmed_reservation()
    request = pending_cancellation(reservation, status=BookingModificationRequest.Status.COMPLETED)
    move_checkout_into_past(reservation)
    client, refunds = Mock(), Mock()
    service = HostawayModificationService(client=client)
    if execution == "automatic":
        outcome = execute_automatic_modification(
            request, service=service, refund_service=refunds, owner_override=True
        )
    else:
        outcome = service.execute(request)
    assert outcome.code == "already_completed"
    assert client.mock_calls == refunds.mock_calls == []


def test_owner_override_does_not_bypass_existing_cancellation_price_guard():
    request = pending_cancellation(confirmed_reservation())
    outcome = execute_automatic_modification(request, service=Mock(), owner_override=True)
    assert outcome.code == "not_price_editable"


def test_elapsed_orphaned_test_payment_still_closes_without_external_operations():
    reservation = confirmed_reservation()
    reservation.normalized_status = Reservation.Status.READY_FOR_HOSTAWAY
    reservation.hostaway_reservation_id = None
    reservation.confirmed_at = None
    reservation.check_out = timezone.localdate() - timedelta(days=1)
    reservation.check_in = reservation.check_out - timedelta(days=2)
    reservation.save()
    payment = PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        provider="hyperpay",
        provider_result_code="000.100.112",
        provider_payment_id="orphaned-completed-stay-test",
        amount=reservation.total_price,
        currency="SAR",
        status=PaymentAttempt.Status.SUCCEEDED,
        verified_at=timezone.now(),
        idempotency_key="orphaned-completed-stay-test-payment-000001",
    )
    with ModificationService() as service:
        request = service.create_cancellation_request(
            reservation,
            session_hash="owner:test",
            owner_override=True,
        ).request
    assert request is not None
    service, refunds = Mock(), Mock()
    outcome = execute_automatic_modification(request, service=service, refund_service=refunds)
    assert outcome.code == "orphaned_test_payment_cancelled"
    assert service.mock_calls == refunds.mock_calls == []
    assert not RefundObligation.objects.exists()
    payment.refresh_from_db()
    assert payment.status == PaymentAttempt.Status.CANCELLED
