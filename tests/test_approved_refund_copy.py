"""Explicit notification-only approval, with synthetic source and provider proof."""

import hashlib
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.notifications.models import AuditLog
from apps.payments.models import PaymentAttempt
from apps.reservations import telegram_booking_events as events
from apps.reservations import telegram_refund_copy_requests as service
from apps.reservations import telegram_website_workgroup as transport
from apps.reservations.models import BookingModificationRequest, RefundObligation
from apps.reservations.tasks import deliver_telegram_booking_event
from apps.reservations.telegram_authority import Denied
from apps.reservations.telegram_stay_models import TelegramWebsiteHandoff
from tests import test_refund_workgroup as source_tests
from tests.test_website_workgroup import proof

pytestmark = pytest.mark.django_db
isolated_settings = source_tests.isolated_settings
refund = source_tests.refund


@pytest.fixture
def request_data(refund):
    booking = refund.reservation
    reference = booking.public_reference.encode()
    modification = refund.modification_request
    return {
        "request_id": str(uuid.uuid4()),
        "reference_md5": hashlib.md5(reference, usedforsecurity=False).hexdigest(),
        "reference_sha256": hashlib.sha256(reference).hexdigest(),
        "check_in": booking.check_in.isoformat(),
        "old_check_out": modification.old_check_out.isoformat(),
        "check_out": booking.check_out.isoformat(),
        "old_total": str(modification.old_total),
        "currency": "SAR",
    }


def old_transfer(refund):
    old = timezone.now() - timedelta(hours=2)
    refund.transferred_at = old
    refund.save(update_fields=["transferred_at"])
    BookingModificationRequest.objects.filter(pk=refund.modification_request_id).update(
        completed_at=old - timedelta(minutes=1)
    )
    PaymentAttempt.objects.update(verified_at=old - timedelta(days=1))
    return old


def test_late_specific_copy_preserves_financial_time_and_queues_once(
    refund, request_data, monkeypatch, django_capture_on_commit_callbacks, capsys
):
    original = old_transfer(refund)
    queued = Mock()
    monkeypatch.setattr(deliver_telegram_booking_event, "delay", queued)
    with django_capture_on_commit_callbacks(execute=True):
        first = service.run_request(json.dumps(request_data))
        second = service.run_request(json.dumps(request_data))
    assert first["decision"] == "queued" and second["decision"] == "existing_planned"
    assert queued.call_count == 1
    row = TelegramWebsiteHandoff.objects.get()
    approval = AuditLog.objects.get(action=service.ACTION)
    assert row.not_before == approval.created_at and row.expires_at > timezone.now()
    assert refund.transferred_at == original
    refund.refresh_from_db()
    assert (
        refund.transferred_at == original
        and refund.amount == 400
        and refund.status == "transferred"
    )
    packet = transport.source_copy("booking_event", row.source_id)
    assert packet["source_revision"] == row.source_revision
    assert packet["not_before"] == approval.created_at.isoformat()
    output = capsys.readouterr().out
    assert request_data["request_id"] in output
    assert (
        refund.public_reference not in output and refund.reservation.public_reference not in output
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("reference_sha256", "f" * 64),
        ("reference_md5", "f" * 32),
        ("old_total", "1"),
        ("check_out", "2000-01-01"),
        ("currency", "USD"),
    ],
)
def test_wrong_target_scope_creates_no_approval_or_message(refund, request_data, field, value):
    request_data[field] = value
    with pytest.raises((Denied, ValueError)):
        service.run_request(json.dumps(request_data))
    assert not AuditLog.objects.filter(action=service.ACTION).exists()
    assert not TelegramWebsiteHandoff.objects.exists()


def test_multiple_matching_refunds_are_not_guessed(refund, request_data):
    RefundObligation.objects.create(
        reservation=refund.reservation,
        modification_request=refund.modification_request,
        reason=refund.reason,
        status=refund.status,
        amount=refund.amount,
        currency=refund.currency,
        transferred_at=refund.transferred_at,
        transfer_reference="second-refund",
        calculation=refund.calculation,
    )
    with pytest.raises(Denied):
        service.run_request(json.dumps(request_data))
    assert not TelegramWebsiteHandoff.objects.exists()


def test_uncertain_existing_attempt_is_status_only_and_never_replaced(
    refund, request_data, monkeypatch
):
    events.queue_event("refund", refund.pk)
    row = TelegramWebsiteHandoff.objects.get()
    row.state = "uncertain"
    row.save(update_fields=["state"])
    queued = Mock()
    monkeypatch.setattr(deliver_telegram_booking_event, "delay", queued)
    result = service.run_request(json.dumps(request_data))
    assert result["decision"] == "existing_uncertain" and not queued.called
    assert TelegramWebsiteHandoff.objects.count() == 1
    assert not AuditLog.objects.filter(action=service.ACTION).exists()


def test_one_handoff_and_repeat_request_return_same_ids_without_post(
    refund, request_data, monkeypatch, django_capture_on_commit_callbacks, capsys
):
    service.run_request(json.dumps(request_data))
    row = TelegramWebsiteHandoff.objects.get()
    calls = []

    def accepted(saved, action):
        calls.append(action)
        transport.authorize({"job": str(saved.pk)}, action, str(saved.pk), {})
        transport.import_proof(saved, proof(saved))

    monkeypatch.setattr(transport, "call", accepted)
    with django_capture_on_commit_callbacks(execute=True):
        assert events.dispatch(row.pk) == "handed_off"
        repeated = service.run_request(
            json.dumps({**request_data, "request_id": str(uuid.uuid4())})
        )
    assert repeated["decision"] == "existing_handed_off"
    assert repeated["provider_message_ids"] == ["101", "102"]
    assert calls == ["website_handoff"]
    output = capsys.readouterr().out
    assert '"guest_accepted": false' in output
    assert '"provider_message_ids": ["101", "102"]' in output


def test_changed_financial_evidence_invalidates_approval_without_new_post(
    refund, request_data, monkeypatch
):
    service.run_request(json.dumps(request_data))
    row = TelegramWebsiteHandoff.objects.get()
    refund.calculation["hyperpay_refund"]["result_description"] = "changed"
    refund.save(update_fields=["calculation"])
    called = Mock()
    monkeypatch.setattr(transport, "call", called)
    assert events.dispatch(row.pk) == "held" and not called.called


def test_unarmed_post_migrate_is_noop_and_invalid_scope_is_private(settings, capsys):
    settings.TELEGRAM_APPROVED_REFUND_COPY_REQUEST = ""
    service.after_migrate(SimpleNamespace(label="reservations"))
    assert capsys.readouterr().out == ""
    settings.TELEGRAM_APPROVED_REFUND_COPY_REQUEST = json.dumps({"request_id": "private-not-an-id"})
    service.after_migrate(SimpleNamespace(label="reservations"))
    output = capsys.readouterr().out
    assert "private-not-an-id" not in output and '"request_id": "invalid"' in output


def test_paused_sender_does_not_record_an_approval(refund, request_data, settings):
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
    with pytest.raises(Denied):
        service.run_request(json.dumps(request_data))
    assert not AuditLog.objects.filter(action=service.ACTION).exists()


@pytest.fixture
def preclaim_hold(refund, request_data):
    service.run_request(json.dumps(request_data))
    row = TelegramWebsiteHandoff.objects.get()
    row.state = "held"
    row.save(update_fields=["state"])
    return row


@pytest.fixture
def absent_native(monkeypatch):
    calls = []

    class Backend:
        authority_context = None

        def call(self, action, key):
            assert action == "website_handoff_status"
            transport.authorize(self.authority_context, action, key, {})
            calls.append(action)
            return {"available": False}

        def close(self):
            pass

    monkeypatch.setattr(transport, "Backend", Backend)
    return calls


def test_operator_can_continue_preclaim_hold_once_without_changing_identity_or_finances(
    refund, request_data, preclaim_hold, absent_native, monkeypatch
):
    row = preclaim_hold
    original = transport.metadata(row)
    financial_time = refund.transferred_at
    old_clock = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: old_clock + timedelta(hours=2))
    assert (
        service.run_request(json.dumps(request_data))["decision"] == "continued_preclaim_same_job"
    )
    row.refresh_from_db()
    assert transport.metadata(row) == original and row.state == "planned"
    assert row.not_before == timezone.now() and row.expires_at > timezone.now()
    assert AuditLog.objects.filter(action=service.ACTION).count() == 2
    assert absent_native == ["website_handoff_status"]
    refund.refresh_from_db()
    assert refund.transferred_at == financial_time and refund.amount == 400
    # The worker continuation cannot loop; only a final source-process attempt
    # is allowed, with another no-slot read and no evidence of any provider POST.
    row.state = "held"
    row.save(update_fields=["state"])
    assert service.run_request(json.dumps(request_data))["decision"] == "source_preclaim_same_job"
    row.refresh_from_db()
    row.state = "held"
    row.save(update_fields=["state"])
    assert service.run_request(json.dumps(request_data))["decision"] == "existing_held"
    assert absent_native == ["website_handoff_status", "website_handoff_status"]


def test_final_source_dispatch_keeps_original_identity_and_records_one_copy(
    refund,
    request_data,
    preclaim_hold,
    absent_native,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    row = preclaim_hold
    original = transport.metadata(row)
    transferred = refund.transferred_at
    service.run_request(json.dumps(request_data))
    row.refresh_from_db()
    row.state = "held"
    row.save(update_fields=["state"])
    posts = []

    def accepted(saved, action):
        assert transport.metadata(saved) == original
        transport.authorize({"job": str(saved.pk)}, action, str(saved.pk), {})
        posts.append(action)
        transport.import_proof(saved, proof(saved))

    monkeypatch.setattr(transport, "call", accepted)
    with django_capture_on_commit_callbacks(execute=True):
        result = service.run_request(json.dumps(request_data))
    assert result["decision"] == "source_preclaim_same_job"
    row.refresh_from_db()
    assert row.state == "handed_off" and transport.metadata(row) == original
    assert service.run_request(json.dumps(request_data))["decision"] == "existing_handed_off"
    assert events.dispatch(row.pk) == "handed_off"
    assert posts == ["website_handoff"] and len(absent_native) == 2
    refund.refresh_from_db()
    assert refund.transferred_at == transferred and refund.amount == 400


@pytest.mark.parametrize(
    "field,value", [("attempt_id", uuid.uuid4()), ("provider_message_id", "123"), ("probes", 1)]
)
def test_any_attempt_evidence_forbids_preclaim_continuation(
    refund, request_data, preclaim_hold, absent_native, field, value
):
    setattr(preclaim_hold, field, value)
    preclaim_hold.save(update_fields=[field])
    assert service.run_request(json.dumps(request_data))["decision"] == "existing_held"
    assert not absent_native
    assert AuditLog.objects.filter(action=service.ACTION).count() == 1


def test_existing_native_slot_is_never_reposted(refund, request_data, preclaim_hold, monkeypatch):
    client = Mock()
    client.call.return_value = {"available": True}
    monkeypatch.setattr(transport, "Backend", lambda: client)
    assert (
        service.run_request(json.dumps(request_data))["decision"] == "native_slot_present_no_repost"
    )
    preclaim_hold.refresh_from_db()
    assert preclaim_hold.state == "held"
    assert AuditLog.objects.filter(action=service.ACTION).count() == 1


def test_unconfirmed_native_lookup_keeps_hold(refund, request_data, preclaim_hold, monkeypatch):
    client = Mock()
    client.call.side_effect = transport.Unavailable
    monkeypatch.setattr(transport, "Backend", lambda: client)
    with pytest.raises(Denied):
        service.run_request(json.dumps(request_data))
    preclaim_hold.refresh_from_db()
    assert preclaim_hold.state == "held"
