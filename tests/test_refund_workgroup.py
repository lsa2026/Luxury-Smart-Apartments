"""Verified refund copies; synthetic records and bridge only, never a payment/send."""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.notifications.services.events import handle_refund_submitted
from apps.payments.models import PaymentAttempt
from apps.reservations import telegram_booking_events as events
from apps.reservations import telegram_website_workgroup as transport
from apps.reservations.models import (
    BookingModificationRequest,
    HostawayModificationOperation,
    RefundObligation,
)
from apps.reservations.tasks import deliver_telegram_booking_event
from apps.reservations.telegram_authority import Denied
from apps.reservations.telegram_stay_models import TelegramWebsiteHandoff
from tests import test_reservation_payment_links as payments
from tests.test_website_workgroup import proof

pytestmark = pytest.mark.django_db
isolated_settings = payments.isolated_settings


@pytest.fixture
def refund(isolated_settings, settings):
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = True
    settings.TELEGRAM_WORKGROUP_CUTOVER_AT = (timezone.now() - timedelta(days=1)).isoformat()
    settings.MODIFICATION_NOTIFICATION_EMAIL_ENABLED = False
    settings.ADMIN_NOTIFICATION_EMAIL_ENABLED = False
    settings.HYPERPAY_ENVIRONMENT = "production"
    booking = payments.manual_stay()
    intent = booking.booking_intent
    booking.is_test = False
    booking.normalized_status = "modified"
    booking.save(update_fields=["is_test", "normalized_status"])
    # The booking may predate activation. Only the actual new refund event is admitted.
    type(booking).objects.filter(pk=booking.pk).update(
        created_at=timezone.now() - timedelta(days=4)
    )
    modification = BookingModificationRequest.objects.create(
        reservation=booking,
        request_type="change_dates",
        status="completed",
        old_check_in=booking.check_in,
        old_check_out=booking.check_out + timedelta(days=5),
        old_guests=booking.guests,
        new_check_in=booking.check_in,
        new_check_out=booking.check_out,
        new_guests=booking.guests,
        old_total=booking.total_price + Decimal("400"),
        new_total=booking.total_price,
        currency=booking.currency,
        price_difference=Decimal("-400"),
        idempotency_key=uuid.uuid4().hex,
        session_key_hash="test",
        completed_at=timezone.now(),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    HostawayModificationOperation.objects.create(
        modification_request=modification,
        operation_type="update_reservation",
        status="succeeded",
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint="a" * 64,
        hostaway_reservation_id=booking.hostaway_reservation_id,
    )
    PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="hyperpay",
        provider_reference="synthetic-original",
        provider_payment_id="synthetic-payment",
        merchant_transaction_id="synthetic-merchant",
        amount=Decimal("1000"),
        currency="SAR",
        status="partially_refunded",
        verified_at=timezone.now(),
        idempotency_key=uuid.uuid4().hex,
    )
    return RefundObligation.objects.create(
        reservation=booking,
        modification_request=modification,
        reason="modification_decrease",
        status="transferred",
        amount=Decimal("400"),
        currency="SAR",
        transfer_reference="synthetic-refund",
        transferred_at=timezone.now(),
        calculation={
            "hyperpay_refund": {
                "state": "confirmed",
                "provider_refund_id": "synthetic-refund",
                "original_payment_id": "synthetic-payment",
                "amount": "400",
                "currency": "SAR",
            }
        },
    )


def source(refund):
    return transport.source_copy("booking_event", "refund:" + str(refund.pk))


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_shortening_and_confirmed_refund_copy_is_accurate_and_preserves_original_logo(
    refund, language
):
    intent = refund.reservation.booking_intent
    intent.language = language
    intent.save(update_fields=["language"])
    packet = source(refund)
    assert packet["purpose"] == "refund_confirmation" and packet["method"] == "photo"
    assert packet["language"] == language and packet["asset_base64"]
    assert refund.reservation.check_out.isoformat() in packet["caption_html"]
    assert refund.public_reference in packet["caption_html"]
    assert packet["not_before"] == refund.transferred_at.isoformat()
    assert {"ar": "تم تعديل الحجز", "en": "Reservation updated", "fr": "Réservation mise à jour"}[
        language
    ] in packet["caption_html"]
    assert not any(
        x in packet["caption_html"]
        for x in ["تم إلغاء الحجز", "Reservation cancelled", "Réservation annulée"]
    )
    assert not TelegramWebsiteHandoff.objects.exists()


@pytest.mark.parametrize(
    "failure",
    [
        "due",
        "processing",
        "unknown",
        "missing_reference",
        "unverified_payment",
        "stale_booking",
        "unconfirmed_modification",
        "old_event",
        "future_event",
        "test_booking",
    ],
)
def test_missing_or_stale_financial_and_booking_evidence_is_held(refund, settings, failure):
    if failure in {"due", "processing"}:
        refund.status = failure
    elif failure == "unknown":
        refund.calculation["hyperpay_refund"]["state"] = "unknown"
    elif failure == "missing_reference":
        refund.transfer_reference = ""
    elif failure == "unverified_payment":
        PaymentAttempt.objects.update(verified_at=None)
    elif failure == "stale_booking":
        booking = refund.reservation
        booking.check_out += timedelta(days=1)
        booking.save(update_fields=["check_out"])
    elif failure == "unconfirmed_modification":
        HostawayModificationOperation.objects.update(status="unknown")
    elif failure == "old_event":
        settings.TELEGRAM_WORKGROUP_CUTOVER_AT = (timezone.now() + timedelta(minutes=1)).isoformat()
    elif failure == "future_event":
        refund.transferred_at = timezone.now() + timedelta(minutes=1)
    elif failure == "test_booking":
        type(refund.reservation).objects.filter(pk=refund.reservation_id).update(is_test=True)
    refund.save()
    with pytest.raises(Denied):
        source(refund)


def test_event_callback_persists_once_before_queue_even_when_email_disabled(
    refund, monkeypatch, django_capture_on_commit_callbacks
):
    queued = Mock()
    monkeypatch.setattr(deliver_telegram_booking_event, "delay", queued)
    with django_capture_on_commit_callbacks(execute=True):
        handle_refund_submitted(refund.pk)
        handle_refund_submitted(refund.pk)
    row = TelegramWebsiteHandoff.objects.get()
    assert row.state == "planned" and queued.call_count == 1
    assert row.source_id == f"refund:{refund.pk}"
    assert "caption_html" not in str(row.__dict__) and "guest_phone" not in str(row.__dict__)


def test_claim_handoff_once_and_unknown_only_reconciles_status(
    refund, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(deliver_telegram_booking_event, "delay", lambda *a: None)
    with django_capture_on_commit_callbacks(execute=True):
        handle_refund_submitted(refund.pk)
    row = TelegramWebsiteHandoff.objects.get()
    calls = []

    def call(saved, action):
        calls.append(action)
        saved.refresh_from_db()
        assert saved.state in {"dispatching", "uncertain"}
        if action == "website_handoff":
            TelegramWebsiteHandoff.objects.filter(pk=saved.pk).update(state="uncertain")
        else:
            transport.import_proof(saved, proof(saved))

    monkeypatch.setattr(transport, "call", call)
    assert events.dispatch(row.pk) == "uncertain"
    assert events.dispatch(row.pk) == "uncertain"
    # Expired/corrected source cannot authorize another send; the old receipt remains readable.
    refund.status = "cancelled"
    refund.save(update_fields=["status"])
    transport.recover()
    row.refresh_from_db()
    assert row.state == "handed_off" and calls == ["website_handoff", "website_handoff_status"]
    refund.refresh_from_db()
    assert refund.status == "cancelled" and refund.amount == Decimal("400")


def test_registration_survives_a_lost_queue_and_uses_existing_minute_recovery(
    refund, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(
        deliver_telegram_booking_event, "delay", Mock(side_effect=RuntimeError("queue unavailable"))
    )
    with django_capture_on_commit_callbacks(execute=True):
        events.queue_event("refund", refund.pk)
    row = TelegramWebsiteHandoff.objects.get()
    calls = []

    def accepted(saved, action):
        calls.append(action)
        transport.import_proof(saved, proof(saved))

    monkeypatch.setattr(transport, "call", accepted)
    transport.recover()
    row.refresh_from_db()
    assert row.state == "handed_off" and calls == ["website_handoff"]


def test_changed_source_before_claim_does_not_dispatch(refund, monkeypatch):
    events.queue_event("refund", refund.pk)
    row = TelegramWebsiteHandoff.objects.get()
    refund.amount += Decimal("1")
    refund.save(update_fields=["amount"])
    call = Mock()
    monkeypatch.setattr(transport, "call", call)
    assert events.dispatch(row.pk) == "held" and not call.called


def test_cancellation_is_named_only_after_final_cancelled_state(refund):
    booking = refund.reservation
    booking.normalized_status = "cancelled"
    booking.save(update_fields=["normalized_status"])
    modification = refund.modification_request
    modification.request_type = "cancel_reservation"
    modification.save(update_fields=["request_type"])
    assert "تم إلغاء الحجز" in source(refund)["caption_html"]
    booking.normalized_status = "modified"
    booking.save(update_fields=["normalized_status"])
    with pytest.raises(Denied):
        source(refund)


def test_manual_transfer_requires_matching_operator_audit_not_a_paid_flag(refund):
    from django.contrib.auth import get_user_model

    from apps.notifications.models import AuditLog

    operator = get_user_model().objects.create_user(username="refund-test-operator")
    refund.calculation = {}
    refund.transferred_by = operator
    refund.save(update_fields=["calculation", "transferred_by"])
    with pytest.raises(Denied):
        source(refund)
    audit = AuditLog.objects.create(
        action="refund.marked_transferred",
        object_type="RefundObligation",
        object_reference=refund.public_reference,
        actor_user=operator,
        metadata={"amount": "400.0000", "currency": "SAR"},
    )
    assert source(refund)["purpose"] == "refund_confirmation"
    audit.metadata["amount"] = "399"
    audit.save(update_fields=["metadata"])
    with pytest.raises(Denied):
        source(refund)


def test_refund_without_modification_never_claims_a_date_change(refund):
    refund.modification_request = None
    refund.save(update_fields=["modification_request"])
    body = source(refund)["caption_html"]
    assert "تأكيد الاسترداد" in body and "تم تعديل الحجز" not in body


def test_paused_refund_copy_does_not_register_or_fall_back_to_guest(refund, settings):
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
    events.queue_event("refund", refund.pk)
    assert not TelegramWebsiteHandoff.objects.exists()


def test_non_refund_namespaces_are_not_accepted(refund):
    with pytest.raises(Denied):
        transport.source_copy(
            "booking_event", "modification:" + str(refund.modification_request_id)
        )
