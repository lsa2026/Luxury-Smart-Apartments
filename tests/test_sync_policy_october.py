"""Website-owned content and future-only accounting policy regressions."""

from datetime import timedelta
from decimal import Decimal
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


@pytest.fixture
def foreign_receipt_settings(settings):
    from tests.test_hyperpay import HYPERPAY_SETTINGS

    for key, value in HYPERPAY_SETTINGS.items():
        setattr(settings, key, value)
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED = True
    settings.HOSTAWAY_FINANCIAL_RECEIPTS_START_AT = "2020-01-01T00:00:00+00:00"
    with (
        patch("httpx.Client.request", side_effect=AssertionError("unexpected live request")),
        patch("apps.payments.hostaway_ledger.alert_operations"),
    ):
        yield


def foreign_paid_reservation(source_amount=Decimal("1000")):
    from tests.test_multi_currency_integration import create_flow

    _, _, intent, _ = create_flow(
        source_amount=source_amount,
        source_currency="MAD",
        display_currency="USD",
        local_currency="MAD",
    )
    reservation = prepare_local_reservation(intent)
    reservation.hostaway_reservation_id = 900112
    reservation.normalized_status = "confirmed"
    reservation.payment_status = "paid"
    reservation.save()
    payment = PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="hyperpay",
        amount=intent.payment_amount_sar,
        currency="SAR",
        status="succeeded",
        verified_at=timezone.now(),
        provider_payment_id="synthetic-foreign-payment",
        idempotency_key="foreign-receipt-test",
    )
    return reservation, payment


def foreign_receipt_client(reservation, payment, *, already_remote=False):
    client = Mock()
    client.get_reservation.return_value = Mock(
        reservation_id=reservation.hostaway_reservation_id,
        listing_map_id=reservation.hostaway_listing_map_id,
        currency="MAD",
    )
    charge = {
        "id": 124,
        "title": f"LSA HyperPay receipt {payment.pk}",
        "amount": str(reservation.total_price),
        "currency": "MAD",
        "status": "paid",
    }
    client.get_guest_charges.return_value = [charge] if already_remote else []
    client.create_offline_paid_charge.return_value = charge
    manager = Mock(__enter__=Mock(return_value=client), __exit__=Mock(return_value=None))
    return client, manager


@pytest.mark.parametrize("already_remote", [False, True])
def test_direct_marrakech_receipt_uses_frozen_source_total_once(
    foreign_receipt_settings, already_remote
):
    reservation, payment = foreign_paid_reservation()
    entry = HostawayFinancialEntry.objects.create(payment_attempt=payment)
    client, manager = foreign_receipt_client(reservation, payment, already_remote=already_remote)
    with (
        patch("apps.payments.hostaway_ledger.HostawayClient", return_value=manager),
        patch("apps.payments.currency.ExchangeRateProvider.fetch") as rates,
    ):
        assert record_payment_receipt_task.run(entry.pk)["status"] == "succeeded"
        assert record_payment_receipt_task.run(entry.pk)["status"] == "already_recorded"
    rates.assert_not_called()
    assert client.create_offline_paid_charge.call_count == (0 if already_remote else 1)
    if not already_remote:
        assert client.create_offline_paid_charge.call_args.kwargs["amount"] == Decimal("1000")
        assert client.create_offline_paid_charge.call_args.kwargs["currency"] == "MAD"
    payment.refresh_from_db()
    assert payment.amount == Decimal("400") and payment.currency == "SAR"
    assert payment.payment_request_id is None
    reservation.refresh_from_db()
    assert reservation.total_price == Decimal("1000") and reservation.payment_status == "paid"


def test_source_total_is_not_reconstructed_from_rounded_sar(foreign_receipt_settings):
    reservation, payment = foreign_paid_reservation(Decimal("1000.01"))
    assert payment.amount == Decimal("400.00")
    entry = HostawayFinancialEntry.objects.create(payment_attempt=payment)
    client, manager = foreign_receipt_client(reservation, payment)
    with patch("apps.payments.hostaway_ledger.HostawayClient", return_value=manager):
        assert record_payment_receipt_task.run(entry.pk)["status"] == "succeeded"
    assert client.create_offline_paid_charge.call_args.kwargs["amount"] == Decimal("1000.01")


def test_verified_direct_gateway_flow_queues_correct_foreign_receipt(
    foreign_receipt_settings, django_capture_on_commit_callbacks
):
    from apps.payments.hyperpay.service import HyperPayService
    from tests.test_hyperpay import HyperPayStub
    from tests.test_reservation_payment_links import approved_document

    reservation, payment = foreign_paid_reservation()
    payment.status = "pending"
    payment.verified_at = None
    payment.provider_payment_id = None
    payment.provider_checkout_id = "ABC123456789.uat01-vm-tx04"
    payment.save()
    gateway = HyperPayStub()
    gateway.payment_document = approved_document(payment)
    with (
        patch("apps.payments.hostaway_ledger._dispatch_receipt") as dispatch,
        django_capture_on_commit_callbacks(execute=True),
    ):
        result = HyperPayService(client=gateway).verify(payment)
    assert result.reservation.pk == reservation.pk
    payment.refresh_from_db()
    assert payment.status == "succeeded" and payment.verified_at
    entry = HostawayFinancialEntry.objects.get(payment_attempt=payment)
    dispatch.assert_called_once_with(entry.pk)
    client, manager = foreign_receipt_client(reservation, payment)
    with patch("apps.payments.hostaway_ledger.HostawayClient", return_value=manager):
        assert record_payment_receipt_task.run(entry.pk)["status"] == "succeeded"
    assert client.create_offline_paid_charge.call_args.kwargs["amount"] == Decimal("1000")
    assert client.create_offline_paid_charge.call_args.kwargs["currency"] == "MAD"
    assert PaymentAttempt.objects.count() == Reservation.objects.count() == 1


def test_foreign_receipt_timeout_never_retries_the_write(foreign_receipt_settings):
    reservation, payment = foreign_paid_reservation()
    entry = HostawayFinancialEntry.objects.create(payment_attempt=payment)
    client, manager = foreign_receipt_client(reservation, payment)
    client.create_offline_paid_charge.side_effect = HostawayTimeoutError("synthetic")
    with patch("apps.payments.hostaway_ledger.HostawayClient", return_value=manager):
        assert record_payment_receipt_task.run(entry.pk)["status"] == "unknown"
        assert record_payment_receipt_task.run(entry.pk)["status"] == "review"
    client.create_offline_paid_charge.assert_called_once()


@pytest.mark.parametrize(
    "problem",
    [
        "partial",
        "overpayment",
        "changed_total",
        "changed_currency",
        "points",
        "missing_fx",
        "bad_fx",
    ],
)
def test_uncertain_foreign_cash_never_posts_full_booking_total(foreign_receipt_settings, problem):
    reservation, payment = foreign_paid_reservation()
    intent = payment.booking_intent
    if problem == "partial":
        payment.amount /= 2
        payment.save()
    elif problem == "overpayment":
        payment.amount += 1
        payment.save()
    elif problem == "changed_total":
        reservation.total_price += 1
        reservation.save()
    elif problem == "changed_currency":
        intent.currency = "EUR"
        intent.save()
    elif problem == "points":
        reservation.loyalty_state = {"redemption": {"value": "100"}}
        reservation.save()
    elif problem == "missing_fx":
        intent.exchange_rate_snapshot = {}
        intent.save()
    else:
        intent.exchange_rate_snapshot["source_amount"] = "1001"
        intent.save()
    entry = HostawayFinancialEntry.objects.create(payment_attempt=payment)
    with patch("apps.payments.hostaway_ledger.HostawayClient") as hostaway:
        assert record_payment_receipt_task.run(entry.pk)["status"] == "review"
    hostaway.assert_not_called()
    payment.refresh_from_db()
    assert payment.status == "succeeded"


def test_foreign_modification_cannot_be_recorded_as_full_initial_payment(foreign_receipt_settings):
    from apps.reservations.models import BookingModificationRequest

    reservation, payment = foreign_paid_reservation()
    modification = BookingModificationRequest.objects.create(
        reservation=reservation,
        request_type="change_guests",
        old_check_in=reservation.check_in,
        old_check_out=reservation.check_out,
        old_guests=reservation.guests,
        old_total=reservation.total_price,
        currency="MAD",
        idempotency_key="foreign-modification-receipt",
        session_key_hash="a" * 64,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    payment.modification_request = modification
    payment.save()
    entry = HostawayFinancialEntry.objects.create(payment_attempt=payment)
    with patch("apps.payments.hostaway_ledger.HostawayClient") as hostaway:
        assert record_payment_receipt_task.run(entry.pk)["status"] == "review"
    hostaway.assert_not_called()


def test_sar_points_receipt_keeps_only_cash_collected(foreign_receipt_settings):
    reservation, payment = paid_reservation()
    reservation.loyalty_state = {"redemption": {"value": "100"}}
    reservation.save()
    payment.amount -= 100
    payment.save()
    entry = HostawayFinancialEntry.objects.create(payment_attempt=payment)
    client, manager = foreign_receipt_client(reservation, payment)
    client.get_reservation.return_value.currency = "SAR"
    with patch("apps.payments.hostaway_ledger.HostawayClient", return_value=manager):
        assert record_payment_receipt_task.run(entry.pk)["status"] == "succeeded"
    assert client.create_offline_paid_charge.call_args.kwargs["amount"] == payment.amount
    assert client.create_offline_paid_charge.call_args.kwargs["currency"] == "SAR"


@pytest.mark.parametrize("entry_status", ["posting", "unknown", "review"])
def test_foreign_receipt_never_reposts_an_uncertain_existing_entry(
    foreign_receipt_settings, entry_status
):
    reservation, payment = foreign_paid_reservation()
    entry = HostawayFinancialEntry.objects.create(payment_attempt=payment, status=entry_status)
    client, manager = foreign_receipt_client(reservation, payment)
    with patch("apps.payments.hostaway_ledger.HostawayClient", return_value=manager):
        assert record_payment_receipt_task.run(entry.pk)["status"] in {"review", "unknown"}
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
