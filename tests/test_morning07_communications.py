"""Synthetic stays and HTTP fakes only: no guest, contact or provider traffic."""

from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.payments.models import MorningPaymentAttempt
from apps.reservations.models import MorningCommunicationEvent, MorningStay
from apps.reservations.morning07_changes import cancel_stay, change_paid_stay
from apps.reservations.morning07_communications import deliver_pending_events, event_token
from apps.reservations.morning_stays import MorningStayError
from tests.test_morning_stays import create

pytestmark = pytest.mark.django_db


@pytest.fixture
def enabled(settings):
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.MORNING07_N8N_ENABLED = True
    settings.MORNING07_N8N_WEBHOOK_URL = "https://lsa25.app.n8n.cloud/webhook/lsa07-morning-stay-v1"
    settings.SITE_BASE_URL = "https://testserver"


def paid_stay():
    stay = create()
    MorningPaymentAttempt.objects.create(
        stay=stay,
        merchant_transaction_id="synthetic-morning-payment",
        provider_payment_id="synthetic-verified-provider-id",
        amount=stay.amount_sar,
        status="succeeded",
        verified_at=timezone.now(),
    )
    stay.status, stay.paid_at = "paid", timezone.now()
    stay.save()
    stay.refresh_from_db()
    return stay


def test_only_verified_confirmed_admission_emits_and_duplicate_save_is_durable(enabled):
    stay = create()
    stay.status, stay.paid_at = "paid", timezone.now()
    stay.save()
    assert not MorningCommunicationEvent.objects.exists()
    MorningPaymentAttempt.objects.create(
        stay=stay,
        merchant_transaction_id="synthetic-reviewed-payment",
        amount=stay.amount_sar,
        provider_payment_id="synthetic-id-review",
        status="succeeded",
        verified_at=timezone.now(),
    )
    stay.status = "review"
    stay.save()
    assert not MorningCommunicationEvent.objects.exists()
    stay.status = "paid"
    stay.save()
    stay.save()
    event = MorningCommunicationEvent.objects.get()
    assert event.revision == 1
    assert event.payload["arrival_date"] == event.payload["departure_date"]
    assert event.payload["check_in_time"] == "01:00"
    assert event.payload["check_out_time"] == "11:00"
    assert event.payload["reservation_key"] == "morning07:" + str(stay.pk)


def test_transaction_rollback_cannot_leak_payment_confirmation(enabled):
    stay = create()
    with pytest.raises(RuntimeError), transaction.atomic():
        MorningPaymentAttempt.objects.create(
            stay=stay,
            merchant_transaction_id="synthetic-rollback",
            amount=stay.amount_sar,
            provider_payment_id="synthetic-rollback-id",
            status="succeeded",
            verified_at=timezone.now(),
        )
        stay.status, stay.paid_at = "paid", timezone.now()
        stay.save()
        raise RuntimeError("rollback")
    assert not MorningCommunicationEvent.objects.exists()
    assert MorningStay.objects.get(pk=stay.pk).status == "open"


def test_same_day_phone_change_and_cancel_create_ordered_revisions(enabled):
    stay = paid_stay()
    values = {
        field: getattr(stay, field)
        for field in (
            "property",
            "starts_at",
            "ends_at",
            "guest_name",
            "guest_phone",
            "guest_email",
            "guest_language",
            "guests",
        )
    }
    values["guest_phone"] = "+16502530001"
    changed = change_paid_stay(stay.pk, values, expected_updated_at=stay.updated_at.isoformat())
    assert changed.amount_sar == stay.amount_sar
    assert list(stay.communication_events.order_by("revision").values_list("state", flat=True)) == [
        "superseded",
        "pending",
    ]
    with pytest.raises(MorningStayError, match="تغير"):
        change_paid_stay(stay.pk, values, expected_updated_at=stay.updated_at.isoformat())
    cancel_stay(stay.pk)
    event = stay.communication_events.get(revision=3)
    assert event.payload["event_type"] == "cancelled"
    assert stay.attempts.get().status == "succeeded"


def test_event_read_requires_its_signed_capability_and_returns_latest(enabled, client):
    stay = paid_stay()
    first = stay.communication_events.get()
    url = reverse("integrations:morning07_event", args=[first.pk])
    assert client.get(url).status_code == 404
    assert client.get(url, HTTP_X_LSA_MORNING07_TOKEN="forged").status_code == 404
    stay.guest_phone = "+16502530001"
    stay.save()
    response = client.get(url, HTTP_X_LSA_MORNING07_TOKEN=event_token(first))
    assert response.status_code == 200
    assert response.json()["source_revision"] == 2
    assert response.json()["phone"] == "+16502530001"
    assert "no-store" in response["Cache-Control"]


def test_transport_timeout_or_plain_200_is_not_acknowledged_and_retry_keeps_id(enabled):
    stay = paid_stay()
    event = stay.communication_events.get()
    remote = Mock()
    remote.post.return_value.status_code = 200
    remote.post.return_value.json.return_value = {"message": "Workflow was started"}
    assert deliver_pending_events(client=remote) == {"accepted": 0}
    first_body = remote.post.call_args.kwargs["json"]
    event.refresh_from_db()
    assert event.state == "pending"
    event.next_attempt_at = timezone.now() - timedelta(seconds=1)
    event.save()
    remote.post.return_value.json.return_value = {"accepted": True, "event_id": str(event.pk)}
    assert deliver_pending_events(client=remote) == {"accepted": 1}
    assert remote.post.call_args.kwargs["json"]["event_id"] == first_body["event_id"]
    assert set(first_body) == {"event_id", "event_token"}
    assert deliver_pending_events(client=remote) == {"accepted": 0}


def test_ended_stay_cannot_emit_or_deliver_delayed_confirmation(enabled, client):
    stay = paid_stay()
    event = stay.communication_events.get()
    MorningStay.objects.filter(pk=stay.pk).update(
        starts_at=timezone.now() - timedelta(hours=1),
        ends_at=timezone.now() - timedelta(seconds=1),
    )
    remote = Mock()
    assert deliver_pending_events(client=remote) == {"accepted": 0}
    assert not remote.post.called
    event.refresh_from_db()
    assert event.state == "expired"
    response = client.get(
        reverse("integrations:morning07_event", args=[event.pk]),
        HTTP_X_LSA_MORNING07_TOKEN=event_token(event),
    )
    assert response.status_code == 409
    stay.refresh_from_db()
    stay.guest_name = "Expired synthetic stay"
    stay.save()
    assert stay.communication_events.count() == 1


def test_event_dispatch_waits_for_commit_and_is_scoped_to_one_stay(
    enabled, django_capture_on_commit_callbacks, monkeypatch
):
    queue = Mock()
    monkeypatch.setattr("apps.reservations.tasks.deliver_morning07_events.delay", queue)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        stay = paid_stay()
        stay.save()
        assert not queue.called
    assert len(callbacks) == 1
    callbacks[0]()
    queue.assert_called_once_with(str(stay.pk))


def test_event_dispatch_is_not_queued_after_transaction_rollback(
    enabled, django_capture_on_commit_callbacks, monkeypatch
):
    queue = Mock()
    monkeypatch.setattr("apps.reservations.tasks.deliver_morning07_events.delay", queue)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(RuntimeError), transaction.atomic():
            paid_stay()
            raise RuntimeError("rollback")
    assert callbacks == []
    queue.assert_not_called()
