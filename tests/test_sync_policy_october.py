"""Website-owned content and future-only accounting policy regressions."""

from datetime import timedelta
from unittest.mock import Mock, patch

import httpx
import pytest
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from apps.integrations.health import get_integration_health
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayTimeoutError
from apps.integrations.hostaway.reservation_validators import HostawayReservationSnapshot
from apps.integrations.models import IntegrationSyncRun
from apps.integrations.monitoring import run_display_command
from apps.integrations.tasks import sync_hostaway_properties_task
from apps.integrations.website_reservations import refresh_website_reservations
from apps.notifications.models import EmailDelivery
from apps.notifications.services.events import handle_paid_booking_needs_review
from apps.payments.hostaway_ledger import queue_payment_receipt, record_payment_receipt_task
from apps.payments.models import HostawayFinancialEntry, PaymentAttempt
from apps.reservations.models import Reservation
from apps.reservations.services.hostaway_booking import prepare_local_reservation
from tests.test_hostaway_booking_phase5 import make_intent

pytestmark = pytest.mark.django_db


def paid_reservation():
    intent = make_intent()
    reservation = prepare_local_reservation(intent)
    reservation.hostaway_reservation_id = 900111
    reservation.normalized_status = Reservation.Status.CONFIRMED
    reservation.payment_status = "paid"
    reservation.save()
    payment = PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="hyperpay",
        amount=intent.total_price,
        currency="SAR",
        status="succeeded",
        verified_at=timezone.now(),
        provider_payment_id="synthetic-paid-reference",
        idempotency_key="ledger-test",
    )
    return reservation, payment


@override_settings(HOSTAWAY_AUTO_SYNC_ENABLED=False)
def test_stale_property_task_cannot_overwrite_website_content():
    with patch("apps.integrations.tasks.sync_properties") as sync:
        assert sync_hostaway_properties_task.run()["status"] == "disabled"
    sync.assert_not_called()


def test_failed_calendar_command_is_not_logged_as_success():
    def command(*args, **kwargs):
        kwargs["stdout"].write("Indicative rates: failed=2 aborted=true")

    with patch("apps.integrations.monitoring.call_command", side_effect=command):
        assert (
            run_display_command("refresh_indicative_rates", "price_calendar")["status"] == "failed"
        )
    run = IntegrationSyncRun.objects.get()
    assert run.failed_count == 2


@override_settings(CELERY_SYNC_DISPATCH_ENABLED=True)
def test_health_requires_observed_scheduler_not_config_flag():
    cache.delete("lsa:health:beat-observed")
    with patch("apps.integrations.health.current_app.control.inspect") as inspect:
        inspect.return_value.ping.return_value = {"worker": {"ok": "pong"}}
        assert get_integration_health().beat_status == "not_observed"
        cache.set("lsa:health:beat-observed", "now", timeout=30)
        assert get_integration_health().beat_status == "available"
    cache.delete("lsa:health:beat-observed")


@override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=True)
def test_ota_only_reservations_are_never_requested():
    with patch("apps.integrations.website_reservations.HostawayClient") as client:
        assert refresh_website_reservations()["processed"] == 0
    client.assert_not_called()


@override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=True)
@pytest.mark.parametrize("remote_status", ["cancelled", "modified"])
def test_known_website_cancellation_and_modification_are_reflected(remote_status):
    reservation, _ = paid_reservation()
    reservation.check_in = timezone.localdate() + timedelta(days=30)
    reservation.check_out = reservation.check_in + timedelta(days=reservation.nights)
    reservation.save()
    snapshot = HostawayReservationSnapshot(
        reservation_id=reservation.hostaway_reservation_id,
        listing_map_id=reservation.hostaway_listing_map_id,
        channel_id=2000,
        status=remote_status,
        check_in=reservation.check_in,
        check_out=reservation.check_out,
        guests=reservation.guests,
        currency="SAR",
        total_price=reservation.total_price,
        payment_status="unpaid",
        source="direct",
        updated_at=timezone.now(),
    )
    client = Mock()
    client.get_reservation.return_value = snapshot
    manager = Mock()
    manager.__enter__ = Mock(return_value=client)
    manager.__exit__ = Mock(return_value=None)
    with patch("apps.integrations.website_reservations.HostawayClient", return_value=manager):
        assert refresh_website_reservations()["status"] == "completed"
    reservation.refresh_from_db()
    assert reservation.normalized_status == remote_status
    assert reservation.payment_status == "paid"  # Never discard verified HyperPay payment.
    assert Reservation.objects.count() == 1


@override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=True)
def test_status_timeout_preserves_local_booking():
    reservation, _ = paid_reservation()
    reservation.check_in = timezone.localdate() + timedelta(days=30)
    reservation.check_out = reservation.check_in + timedelta(days=reservation.nights)
    reservation.save()
    client = Mock()
    client.get_reservation.side_effect = HostawayTimeoutError("Synthetic timeout")
    manager = Mock()
    manager.__enter__ = Mock(return_value=client)
    manager.__exit__ = Mock(return_value=None)
    with patch("apps.integrations.website_reservations.HostawayClient", return_value=manager):
        assert refresh_website_reservations()["status"] == "deferred"
    reservation.refresh_from_db()
    assert reservation.normalized_status == Reservation.Status.CONFIRMED


def test_accounting_queue_failure_cannot_break_payment_flow():
    with (
        patch("apps.payments.hostaway_ledger._queue_verified_receipt", side_effect=ValueError),
        patch("apps.integrations.monitoring.dispatch_event", side_effect=RuntimeError),
    ):
        queue_payment_receipt("synthetic")


@override_settings(
    HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED=True,
    HYPERPAY_ENVIRONMENT="production",
    HOSTAWAY_FINANCIAL_RECEIPTS_START_AT="2099-01-01T00:00:00+00:00",
)
def test_historical_payment_does_not_create_receipt():
    _, payment = paid_reservation()
    queue_payment_receipt(payment.pk)
    assert not HostawayFinancialEntry.objects.exists()


@override_settings(
    HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED=True,
    HYPERPAY_ENVIRONMENT="production",
    HOSTAWAY_FINANCIAL_RECEIPTS_START_AT="2020-01-01T00:00:00+00:00",
)
def test_receipt_queue_is_unique_and_requires_verified_payment():
    _, payment = paid_reservation()
    with patch("apps.payments.hostaway_ledger._dispatch_receipt"):
        queue_payment_receipt(payment.pk)
        queue_payment_receipt(payment.pk)
    assert HostawayFinancialEntry.objects.count() == 1


@override_settings(
    HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED=True,
    HYPERPAY_ENVIRONMENT="production",
    HOSTAWAY_FINANCIAL_RECEIPTS_START_AT="2020-01-01T00:00:00+00:00",
)
@pytest.mark.parametrize("already_remote", [False, True])
def test_receipt_is_posted_once_or_recovered_by_marker(already_remote):
    reservation, payment = paid_reservation()
    entry = HostawayFinancialEntry.objects.create(payment_attempt=payment)
    client = Mock()
    client.get_reservation.return_value = Mock(
        reservation_id=reservation.hostaway_reservation_id,
        listing_map_id=reservation.hostaway_listing_map_id,
        currency="SAR",
    )
    charge = {
        "id": 123,
        "title": f"LSA HyperPay receipt {payment.pk}",
        "amount": str(payment.amount),
        "currency": "SAR",
        "status": "paid",
    }
    client.get_guest_charges.return_value = [charge] if already_remote else []
    client.create_offline_paid_charge.return_value = charge
    manager = Mock()
    manager.__enter__ = Mock(return_value=client)
    manager.__exit__ = Mock(return_value=None)
    with patch("apps.payments.hostaway_ledger.HostawayClient", return_value=manager):
        assert record_payment_receipt_task.run(str(entry.pk))["status"] == "succeeded"
        assert record_payment_receipt_task.run(str(entry.pk))["status"] == "already_recorded"
    assert client.create_offline_paid_charge.call_count == (0 if already_remote else 1)


@override_settings(
    HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED=True,
    HYPERPAY_ENVIRONMENT="production",
    HOSTAWAY_FINANCIAL_RECEIPTS_START_AT="2020-01-01T00:00:00+00:00",
)
def test_unknown_receipt_never_blindly_reposts():
    reservation, payment = paid_reservation()
    entry = HostawayFinancialEntry.objects.create(payment_attempt=payment, status="unknown")
    client = Mock()
    client.get_reservation.return_value = Mock(
        reservation_id=reservation.hostaway_reservation_id,
        listing_map_id=reservation.hostaway_listing_map_id,
        currency="SAR",
    )
    client.get_guest_charges.return_value = []
    manager = Mock()
    manager.__enter__ = Mock(return_value=client)
    manager.__exit__ = Mock(return_value=None)
    with patch("apps.payments.hostaway_ledger.HostawayClient", return_value=manager):
        assert record_payment_receipt_task.run(str(entry.pk))["status"] == "review"
    client.create_offline_paid_charge.assert_not_called()


@override_settings(EMAIL_DELIVERY_ENABLED=True, OPERATIONS_EMAIL="owner@example.invalid")
def test_paid_creation_failure_notifies_once_and_does_not_retry(django_capture_on_commit_callbacks):
    reservation, _ = paid_reservation()
    with (
        django_capture_on_commit_callbacks(execute=True),
        patch("apps.notifications.services.email._dispatch_email_delivery"),
    ):
        handle_paid_booking_needs_review(reservation.pk)
        handle_paid_booking_needs_review(reservation.pk)
    assert EmailDelivery.objects.filter(message_type="paid_booking_needs_review").count() == 1


def test_charge_reader_rejects_another_reservation():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "status": "success",
                "result": [{"id": 1, "reservationId": 999}],
            },
        )
    )
    with httpx.Client(transport=transport, base_url="https://api.hostaway.com/v1") as http:
        client = HostawayClient(client=http, access_token="synthetic")
        from apps.integrations.hostaway.exceptions import HostawayResponseError

        with pytest.raises(HostawayResponseError):
            client.get_guest_charges(123)
