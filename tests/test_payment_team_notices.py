"""Unified paid-event notices: isolated DB, fake gateway and fake WhatsApp."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.payments.hyperpay.service import HyperPayService
from apps.payments.models import PaymentAttempt, PaymentTeamNotice, ReservationPaymentRequest
from apps.payments.team_notices import deliver_team_notice, message, queue_notice, recover_notices
from tests.test_hyperpay import HyperPayStub
from tests.test_morning_stays import approved, create, property_and_remote, start_payment
from tests.test_reservation_payment_links import (
    approved_document,
    isolated_settings,  # noqa: F401
    manual_stay,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def team_settings(settings):
    settings.PAYMENT_TEAM_NOTICES_ENABLED = True
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.ULTRAMSG_ENABLED = True
    settings.SITE_BASE_URL = "https://booking.example"
    with patch("apps.payments.team_notices.dispatch"):
        yield


def capture(*, settled=True):
    stay = manual_stay()
    stay.is_test = False
    stay.save(update_fields=["is_test"])
    attempt = PaymentAttempt.objects.create(
        booking_intent=stay.booking_intent,
        provider="hyperpay",
        amount=Decimal("500"),
        currency="SAR",
        merchant_transaction_id=uuid4().hex,
        status="succeeded" if settled else "pending",
        verified_at=timezone.now() if settled else None,
        provider_payment_id=uuid4().hex if settled else None,
    )
    return stay, attempt


def test_queue_is_postcommit_once_and_rollback_never_sends(django_capture_on_commit_callbacks):
    _, attempt = capture()
    with patch("apps.payments.team_notices.dispatch") as dispatch:
        with django_capture_on_commit_callbacks(execute=True):
            queue_notice(attempt)
            queue_notice(attempt)
        assert PaymentTeamNotice.objects.count() == 1
        dispatch.assert_called_once()
        with pytest.raises(ValueError), transaction.atomic():
            PaymentTeamNotice.objects.all().delete()
            queue_notice(attempt)
            raise ValueError("rollback")
        assert PaymentTeamNotice.objects.count() == 1
        dispatch.assert_called_once()


@pytest.mark.parametrize(
    "invalid", ["pending", "failed", "review", "missing_id", "test", "disabled"]
)
def test_only_real_verified_positive_successes_queue(settings, invalid):
    stay, attempt = capture()
    if invalid in {"pending", "failed", "review"}:
        attempt.status = invalid
    elif invalid == "missing_id":
        attempt.provider_payment_id = None
    elif invalid == "test":
        stay.is_test = True
        stay.save()
    else:
        settings.PAYMENT_TEAM_NOTICES_ENABLED = False
    queue_notice(attempt)
    assert not PaymentTeamNotice.objects.exists()


@pytest.mark.parametrize("path", ["ordinary", "payment_link", "revised_link", "modification"])
def test_real_verification_hook_queues_new_success_but_does_not_backfill_old_success(path):
    from tests.test_payment_link_revisions import change

    stay, attempt = capture(settled=False)
    modification = change(stay) if path in {"revised_link", "modification"} else None
    if path in {"payment_link", "revised_link"}:
        attempt.payment_request = ReservationPaymentRequest.objects.create(
            reservation=stay,
            modification_request=modification,
            amount_sar=attempt.amount,
            source_amount=attempt.amount,
            source_currency="SAR",
            fingerprint="synthetic",
            status="paid",
            environment="production",
        )
    elif modification:
        attempt.modification_request = modification
    attempt.save()
    with patch.object(HyperPayService, "_complete_success"):
        HyperPayService(client=Mock())._process_verification(attempt.pk, approved_document(attempt))
    assert PaymentTeamNotice.objects.filter(payment_attempt=attempt).count() == 1
    body = message(PaymentTeamNotice.objects.get())
    assert "500.00 SAR" in body
    if modification:
        assert "تعديل" in body or "المعدّل" in body
    # Reconciliation of an already verified historic success never queues a new notice.
    PaymentTeamNotice.objects.all().delete()
    with patch.object(HyperPayService, "_complete_success"):
        HyperPayService(client=Mock())._process_verification(attempt.pk, approved_document(attempt))
    assert not PaymentTeamNotice.objects.exists()


def test_secondary_queue_failure_never_rolls_back_verified_payment():
    _, attempt = capture(settled=False)
    with (
        patch.object(HyperPayService, "_complete_success"),
        patch.object(
            PaymentTeamNotice.objects, "get_or_create", side_effect=RuntimeError("outbox")
        ),
    ):
        HyperPayService(client=Mock())._process_verification(attempt.pk, approved_document(attempt))
    attempt.refresh_from_db()
    assert attempt.status == "succeeded" and attempt.verified_at


def test_send_is_once_uses_fixed_work_group_and_no_guest_payment_url(settings):
    stay, attempt = capture()
    queue_notice(attempt)
    row = PaymentTeamNotice.objects.get()
    with patch("apps.payments.team_notices.UltraMsgClient") as provider:
        send = provider.return_value.__enter__.return_value.send_text
        send.return_value = {"id": "synthetic-message"}
        assert deliver_team_notice.run(row.pk) == {"status": "accepted"}
        assert deliver_team_notice.run(row.pk) == {"status": "accepted"}
    send.assert_called_once()
    data = send.call_args.kwargs
    assert data["recipient"] == settings.MORNING07_WORK_GROUP_ID
    assert "500.00 SAR" in data["body"]
    assert f"/bookings/{stay.pk}/" in data["body"]
    assert "https://booking.example/payments/" not in data["body"]
    assert stay.booking_intent.guest_email not in data["body"]
    assert stay.booking_intent.guest_phone not in data["body"]


def test_uncertain_send_and_crashed_claim_are_never_reposted():
    _, attempt = capture()
    queue_notice(attempt)
    row = PaymentTeamNotice.objects.get()
    with patch("apps.payments.team_notices.UltraMsgClient") as provider:
        send = provider.return_value.__enter__.return_value.send_text
        send.side_effect = TimeoutError()
        assert deliver_team_notice.run(row.pk) == {"status": "uncertain"}
        assert deliver_team_notice.run(row.pk) == {"status": "uncertain"}
        send.assert_called_once()
        PaymentTeamNotice.objects.filter(pk=row.pk).update(status="sending")
        assert deliver_team_notice.run(row.pk) == {"status": "sending"}
        send.assert_called_once()


def test_recovery_only_dispatches_queued_outbox_not_old_payments():
    _, attempt = capture()
    queue_notice(attempt)
    row = PaymentTeamNotice.objects.get()
    PaymentTeamNotice.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timedelta(minutes=1)
    )
    with patch("apps.payments.team_notices.dispatch") as dispatch:
        recover_notices()
        dispatch.assert_called_once_with(row.pk)
    assert PaymentTeamNotice.objects.count() == 1


def test_morning_verification_uses_same_outbox_and_preserves_independent_journey(settings):
    from apps.payments.morning_payments import verify_payment

    prop, remote = property_and_remote()
    stay = create(prop, remote)
    # Existing helper's fake provider never performs a network call.
    gateway = HyperPayStub()
    attempt = start_payment(stay, remote, gateway)
    gateway.payment_document = approved(attempt)
    verify_payment(attempt.pk, client=gateway)
    row = PaymentTeamNotice.objects.get(morning_attempt=attempt)
    body = message(row)
    assert "حجز صباحي" in body and "Asia/Riyadh" in body
    assert f"/morning-stays/{stay.pk}/" in body
    verify_payment(attempt.pk, client=gateway)
    assert PaymentTeamNotice.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_workers_post_only_one_notice():
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    _, attempt = capture()
    queue_notice(attempt)
    row = PaymentTeamNotice.objects.get()

    def run():
        close_old_connections()
        try:
            return deliver_team_notice.run(row.pk)
        finally:
            close_old_connections()

    with patch("apps.payments.team_notices.UltraMsgClient") as provider:
        send = provider.return_value.__enter__.return_value.send_text
        send.return_value = {"id": "synthetic"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: run(), range(2)))
        send.assert_called_once()
