"""Independent payment proof, staged delivery and legacy sender exclusion."""

import json
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.loyalty.models import JoinInvitation
from apps.notifications.services.ultramsg import UltraMsgConnectionError
from apps.payments.models import MorningPaymentAttempt
from apps.reservations import morning07_payments as service
from apps.reservations import morning_loyalty
from apps.reservations.models import (
    Morning07DeliveryAttempt,
    Morning07PaymentDelivery,
    Morning07PaymentEvent,
    MorningOfflineReceipt,
    MorningStay,
)
from apps.reservations.morning07_gateway import exact_receipt_messages as actual_lookup
from apps.reservations.tasks import invite_paid_morning
from tests.morning07_payment_harness import PaymentHarness
from tests.test_morning_auto_payment import confirmed
from tests.test_morning_hostaway import flags as flags  # noqa: F401
from tests.test_morning_hostaway import request_for
from tests.test_morning_stays import isolated as isolated  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def enabled(settings, isolated):  # noqa: F811
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.MORNING07_N8N_ENABLED = True
    settings.MORNING07_PAYMENT_HANDOFF_ENABLED = True
    settings.MORNING07_PAYMENT_WEBHOOK_URL = "https://automation.example.test/payments"
    settings.WHATSAPP_CONTACT_NUMBER = "+16502530000"
    with (
        patch.object(service, "queue_event"),
        patch("apps.reservations.morning07_communications.queue_stay_event"),
    ):
        yield


@pytest.fixture
def rig():
    with PaymentHarness() as harness:
        yield harness


def test_phased_core_payment_receipt_invitation_sequence_once(rig):
    source = rig.source().json()
    assert source["payment_verified"] is True
    assert source["booking_revision"] == rig.stay.communication_revision
    assert source["proof_kind"] == "hyperpay_verified"
    assert "synthetic-verified-payment" not in json.dumps(source)
    assert rig.send("payment_success").json()["code"] == "core_pending"
    assert not Morning07PaymentDelivery.objects.exists()
    rig.acks["core"] = 2
    assert rig.send("payment_success").json()["code"] == "accepted"
    assert rig.send("payment_success").json()["code"] == "duplicate"
    assert rig.send("loyalty_invitation").json()["code"] == "payment_notice_pending"
    assert Morning07PaymentDelivery.objects.count() == 1
    rig.acks["payment_success"] = 2
    assert rig.receipt("payment_success").json()["messages"][0]["ack"] == 2
    invite = rig.send("loyalty_invitation").json()
    assert invite["code"] == "accepted"
    row = JoinInvitation.objects.get()
    assert invite["provider_reference"] == row.provider_reference
    assert row.state == "accepted"
    rig.acks["loyalty_invitation"] = 3
    assert rig.receipt("loyalty_invitation").json()["code"] == "ok"
    row.refresh_from_db()
    assert row.state == "delivered"
    assert rig.send("loyalty_invitation").json()["code"] == "duplicate"
    assert rig.text.send_text.call_count == rig.image.send_image.call_count == 1
    assert rig.stay.communication_events.count() == rig.stay.payment_events.count() == 1


def test_capabilities_are_event_and_purpose_bound_and_requests_strict(rig):
    assert rig.source(token=service.capability(rig.event, delivery=True)).status_code == 404
    assert rig.send("payment_success", token=service.capability(rig.event)).status_code == 404
    assert rig.send("payment_success", token="invalid").status_code == 404
    body = {
        "message_type": "payment_success",
        **rig.source().json()["jobs"]["payment_success"],
        "message_body": "untrusted",
    }
    assert rig.send("payment_success", body=body).status_code == 400
    assert rig.send("unsupported").status_code == 400
    assert Client().get(reverse("integrations:morning07_payment_send")).status_code == 405
    assert not Morning07PaymentDelivery.objects.exists()


@pytest.mark.parametrize(
    "state", ["failed", "cancelled", "pending", "unknown", "review", "created"]
)
def test_unverified_financial_states_never_emit_or_send(state):
    stay, _ = confirmed()
    MorningPaymentAttempt.objects.create(
        stay=stay,
        merchant_transaction_id="synthetic-negative",
        amount=stay.amount_sar,
        status=state,
        verified_at=timezone.now(),
        provider_payment_id="not-proof",
    )
    stay.status, stay.paid_at = "paid", timezone.now()
    stay.save(update_fields=["status", "paid_at", "updated_at"])
    assert not Morning07PaymentEvent.objects.exists()
    assert not Morning07PaymentDelivery.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "review"},
        {"status": "cancelled"},
        {"environment": "test"},
        {"guest_phone": "+442079460018"},
        {"guest_email": "changed@example.test"},
        {
            "starts_at": timezone.now() - timedelta(hours=2),
            "ends_at": timezone.now() - timedelta(seconds=1),
        },
    ],
)
def test_revoked_or_stale_source_cannot_start_message(rig, changes):
    MorningStay.objects.filter(pk=rig.stay.pk).update(**changes)
    rig.acks["core"] = 2
    assert rig.source().status_code == 409
    assert rig.send("payment_success").status_code == 409
    rig.text.send_text.assert_not_called()


def test_capture_idempotent_and_does_not_replay_base_events(
    rig, django_capture_on_commit_callbacks
):
    with (
        django_capture_on_commit_callbacks(execute=True),
        patch.object(service, "queue_event") as queue,
    ):
        for _ in range(3):
            assert service.capture_payment(rig.stay.pk).pk == rig.event.pk
    queue.assert_not_called()
    assert rig.stay.payment_events.count() == rig.stay.communication_events.count() == 1


def test_manual_receipt_produces_independent_proof(settings):
    from apps.reservations.morning_receipts import record_receipt

    stay, _ = confirmed()
    with patch("apps.reservations.tasks.invite_paid_morning.delay") as old:
        record_receipt(
            request=request_for(stay),
            stay_id=stay.pk,
            amount=stay.amount_sar,
            method="bank_transfer",
            received_at=timezone.now(),
            confirmed=True,
        )
    event = stay.payment_events.get()
    assert event.proof_kind == "admin_receipt" and event.payload["payment_status"] == "paid"
    assert stay.communication_events.count() == 1
    old.assert_not_called()


def test_conflicting_manual_receipt_and_gateway_proof_is_rejected(rig):
    MorningOfflineReceipt.objects.create(
        stay=rig.stay,
        amount=rig.stay.amount_sar,
        method="cash",
        received_at=timezone.now(),
        recorded_by=rig.stay.created_by,
    )
    assert rig.source().status_code == 409
    assert not service.verified_facts(rig.stay)


def test_unknown_provider_is_reconciled_without_a_second_post(rig):
    rig.acks["core"] = 2
    rig.text.send_text.side_effect = UltraMsgConnectionError()
    assert rig.send("payment_success").json()["code"] == "unknown"
    assert rig.send("payment_success").json()["code"] == "duplicate"
    rig.acks["payment_success"] = 2
    assert rig.receipt("payment_success").json()["messages"][0]["ack"] == 2
    assert rig.send("loyalty_invitation").json()["code"] == "accepted"
    assert rig.text.send_text.call_count == 1


def test_invitation_unknown_uses_real_registry_reference_without_repost(rig):
    rig.acks.update(core=2, payment_success=2)
    rig.send("payment_success")
    rig.image.send_image.side_effect = UltraMsgConnectionError()
    outcome = rig.send("loyalty_invitation").json()
    row = JoinInvitation.objects.get()
    assert outcome["code"] == "unknown" and outcome["provider_reference"] == row.provider_reference
    assert rig.send("loyalty_invitation").json()["code"] == "duplicate"
    rig.acks["loyalty_invitation"] = 2
    rig.receipt("loyalty_invitation")
    row.refresh_from_db()
    assert row.state == "delivered" and rig.image.send_image.call_count == 1


def test_legacy_sender_blocked_even_after_flag_rollback(rig, settings):
    assert (
        json.loads(morning_loyalty.invite_paid_booking(rig.stay, 1).content)["code"]
        == "managed_by_morning07"
    )
    assert invite_paid_morning.run(str(rig.stay.pk))["code"] == "managed_by_morning07"
    settings.MORNING07_PAYMENT_HANDOFF_ENABLED = False
    assert (
        json.loads(morning_loyalty.invite_paid_booking(rig.stay, 1).content)["code"]
        == "managed_by_morning07"
    )
    assert rig.source().status_code == 404
    rig.image.send_image.assert_not_called()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_canonical_success_text_uses_actual_local_hours_no_raw_payment_id(rig, language):
    rig.stay.guest_language = language
    text = service.render_success(rig.stay)
    assert "500.00 SAR" in text and "01:00" in text and "11:00" in text
    assert "AM-" + str(rig.stay.pk)[:8] in text
    assert "synthetic-verified-payment" not in text
    assert "tomorrow" not in text and "غدًا" not in text


def test_outbox_ack_requires_exact_event_and_never_contains_guest_fields(rig):
    remote = Mock()
    remote.post.return_value.status_code = 202
    remote.post.return_value.json.return_value = {"accepted": True, "payment_event_id": "wrong"}
    assert service.deliver_event(rig.event.pk, client=remote) == {"accepted": False}
    assert set(remote.post.call_args.kwargs["json"]) == {"payment_event_id", "event_token"}
    Morning07PaymentEvent.objects.filter(pk=rig.event.pk).update(next_attempt_at=timezone.now())
    remote.post.return_value.json.return_value = {
        "accepted": True,
        "payment_event_id": str(rig.event.pk),
    }
    assert service.deliver_event(rig.event.pk, client=remote) == {"accepted": True}
    assert service.deliver_event(rig.event.pk, client=remote) == {"accepted": True}
    assert remote.post.call_count == 2


def test_member_is_skipped_without_creating_membership_or_points(rig):
    rig.acks.update(core=2, payment_success=2)
    rig.send("payment_success")
    with patch.object(morning_loyalty.registry, "member_exists", return_value=True):
        assert rig.send("loyalty_invitation").json()["code"] == "member"
    rig.image.send_image.assert_not_called()
    assert not JoinInvitation.objects.exists()


def test_exact_receipt_filter_ignores_unrelated_delivered_messages(rig):
    from apps.reservations import morning07_gateway

    rig.acks["core"] = 2
    rig.send("payment_success")
    attempt = rig.event.deliveries.get()
    with (
        patch.object(service, "exact_receipt_messages", side_effect=actual_lookup),
        patch.object(morning07_gateway, "UltraMsgClient") as client,
    ):
        remote = client.return_value.__enter__.return_value.http
        remote.get.return_value.json.return_value = [
            {"id": "unrelated", "referenceId": "other-reference", "ack": 3},
            {"id": attempt.provider_id, "referenceId": attempt.provider_reference, "ack": 1},
        ]
        rows = rig.receipt("payment_success").json()["messages"]
        assert len(rows) == 1 and rows[0]["ack"] == 1
        assert rig.send("loyalty_invitation").json()["code"] == "payment_notice_pending"
        remote.get.return_value.json.return_value[1]["ack"] = 2
        assert rig.send("loyalty_invitation").json()["code"] == "accepted"


def test_receipt_is_owned_and_can_reconcile_after_cancellation(rig):
    rig.acks["core"] = 2
    rig.send("payment_success")
    token = service.capability(rig.event, delivery=True)
    url = reverse("integrations:morning07_payment_receipt")
    assert (
        rig.client.get(
            url, {"reference_id": "unowned"}, HTTP_X_LSA_MORNING07_PAYMENT_TOKEN=token
        ).status_code
        == 404
    )
    assert (
        rig.client.get(
            url,
            {"reference_id": "unowned", "id": "arbitrary"},
            HTTP_X_LSA_MORNING07_PAYMENT_TOKEN=token,
        ).status_code
        == 400
    )
    MorningStay.objects.filter(pk=rig.stay.pk).update(status="cancelled")
    rig.acks["payment_success"] = 2
    assert rig.receipt("payment_success").json()["messages"][0]["ack"] == 2
    assert rig.send("loyalty_invitation").status_code == 409


def test_lookup_uncertainty_does_not_claim_a_send(rig):
    rig.acks["core"] = 2
    with patch.object(morning_loyalty, "exact_receipt_messages", side_effect=ValueError):
        assert rig.send("payment_success").json()["code"] == "receipt_lookup_uncertain"
    assert not Morning07PaymentDelivery.objects.exists()


def test_uncertain_core_post_can_proceed_only_after_exact_delivery_receipts(rig):
    Morning07DeliveryAttempt.objects.filter(stay=rig.stay).update(state="uncertain", provider_id="")
    assert rig.send("payment_success").json()["code"] == "core_pending"
    rig.acks["core"] = 2
    assert rig.send("payment_success").json()["code"] == "accepted"


def test_crash_after_invitation_permit_retains_real_reference(rig):
    rig.acks.update(core=2, payment_success=2)
    rig.send("payment_success")
    rig.image.send_image.side_effect = KeyboardInterrupt("synthetic-process-crash")
    with pytest.raises(KeyboardInterrupt):
        rig.send("loyalty_invitation")
    row = JoinInvitation.objects.get()
    attempt = rig.event.deliveries.get(message_type="loyalty_invitation")
    assert attempt.state == "posting" and attempt.provider_reference == row.provider_reference
    rig.acks["loyalty_invitation"] = 2
    rig.receipt("loyalty_invitation")
    assert rig.send("loyalty_invitation").json()["code"] == "duplicate"
    assert rig.image.send_image.call_count == 1


def test_unrelated_save_does_not_backfill_existing_paid_stay(rig, settings):
    # Use the same authoritative proof but an isolated, event-free copy of this case.
    rig.event.delete()
    rig.stay.guest_name = "Synthetic Updated Guest"
    rig.stay.save(update_fields=["guest_name", "updated_at"])
    assert not rig.stay.payment_events.exists()


def test_financial_change_rollback_also_rolls_back_outbox():
    from django.db import transaction

    stay, _ = confirmed()
    with pytest.raises(RuntimeError), transaction.atomic():
        MorningPaymentAttempt.objects.create(
            stay=stay,
            merchant_transaction_id="synthetic-rollback",
            status="succeeded",
            amount=stay.amount_sar,
            verified_at=timezone.now(),
            provider_payment_id="synthetic-rollback-payment",
        )
        stay.paid_at = stay.attempts.get().verified_at
        stay.status = "paid"
        stay.save(update_fields=["status", "paid_at", "updated_at"])
        assert stay.payment_events.exists()
        raise RuntimeError("synthetic-rollback")
    assert not stay.payment_events.exists() and not stay.attempts.exists()


def test_full_journey_from_unpaid_booking_through_real_gateways_and_verified_payment():
    import hashlib

    with PaymentHarness(full_journey=True) as journey:
        stay = journey.stay
        assert not stay.attempts.exists() and not stay.payment_events.exists()
        assert not Morning07DeliveryAttempt.objects.exists()
        source = journey.phase({"action": "booking_source"})["body"]
        assert source["booking_verified"] and not source["payment_verified"]
        assert journey.phase({"action": "source"})["body"]["code"] == "no_payment_event"
        phone = stay.guest_phone.removeprefix("+")
        stamp = int(stay.starts_at.timestamp() * 1000)
        keys = [
            f"lsa07:{stay.pk}:r1:status",
            f"lsa07:{stay.pk}:rules:{phone}",
            f"lsa07:{stay.pk}:guide:{stay.property.hostaway_listing_map_id}:{stamp}:{phone}",
        ]
        for kind, key in zip(["status_new", "house_rules", "checkin_guide"], keys, strict=True):
            reference = "lsa07-" + hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()
            job = {
                "job_key": key,
                "reference_id": reference,
                "message_type": kind,
                "recipient_kind": "guest",
                "recipient_phone": phone,
                "message_body": "Synthetic " + kind,
            }
            assert journey.phase({"action": "booking_send", "job": job})["status"] == 202
            assert (
                journey.phase({"action": "booking_receipt", "reference_id": reference})["body"][
                    "messages"
                ][0]["ack"]
                == 1
            )
        assert journey.phase({"action": "payment_link_request"})["body"]["code"] == "waiting_core"
        assert journey.phase({"action": "payment_link_complete"})["code"] == "core_pending"
        journey.phase({"action": "acks", "values": {"core": 2}})
        assert journey.phase({"action": "payment_link_complete"})["code"] == "accepted"
        assert journey.phase({"action": "payment_link_complete"})["code"] == "accepted"
        assert journey.phase({"action": "verified_payment"})["payment_status"] == "succeeded"
        assert journey.phase({"action": "verified_payment"})["payment_status"] == "succeeded"
        assert journey.send("payment_success").json()["code"] == "accepted"
        assert journey.send("loyalty_invitation").json()["code"] == "payment_notice_pending"
        journey.phase({"action": "acks", "values": {"payment_success": 2}})
        assert journey.send("loyalty_invitation").json()["code"] == "accepted"
        journey.phase({"action": "acks", "values": {"loyalty_invitation": 2}})
        journey.receipt("loyalty_invitation")
        stats = journey.phase({"action": "stats"})
        assert stats == {
            "core_posts": 3,
            "payment_link_posts": 1,
            "text_posts": 1,
            "image_posts": 1,
            "booking_events": 1,
            "payment_events": 1,
            "hostaway_creates": 1,
            "checkout_posts": 1,
            "payment_checks": 1,
        }


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "pending", "unknown", "review"])
def test_full_journey_verification_failure_stays_on_website_only(outcome):
    with PaymentHarness(full_journey=True) as journey:
        result = journey.phase({"action": "verified_payment", "outcome": outcome})
        assert result["payment_status"] == outcome
        assert result["payment_event_id"] is None
        assert journey.send("payment_success").status_code == 409
        assert journey.phase({"action": "stats"})["text_posts"] == 0


@pytest.mark.django_db(transaction=True)
def test_parallel_payment_requests_share_one_committed_provider_claim(rig):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections, connection

    if connection.vendor != "postgresql":
        pytest.skip("Row-lock concurrency requires isolated PostgreSQL CI.")
    rig.acks["core"] = 2
    started, release = Event(), Event()

    def provider_post(**_kwargs):
        started.set()
        assert release.wait(10)
        return {"sent": "true", "id": "synthetic-parallel-success"}

    rig.text.send_text.side_effect = provider_post
    key, ref = service.reference(rig.event.pk, "payment_success")
    token = service.capability(rig.event, delivery=True)

    def post():
        close_old_connections()
        try:
            return (
                Client()
                .post(
                    reverse("integrations:morning07_payment_send"),
                    {"message_type": "payment_success", "job_key": key, "reference_id": ref},
                    content_type="application/json",
                    HTTP_X_LSA_MORNING07_PAYMENT_TOKEN=token,
                )
                .json()
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(post)
        try:
            assert started.wait(10)
            assert executor.submit(post).result(timeout=10)["code"] == "duplicate"
        finally:
            release.set()
        assert first.result(timeout=10)["code"] == "accepted"
    assert rig.text.send_text.call_count == 1


def test_programme_not_ready_can_defer_without_consuming_provider_permit(rig):
    rig.acks.update(core=2, payment_success=2)
    rig.send("payment_success")
    with patch.object(morning_loyalty, "ready", return_value=False):
        response = rig.send("loyalty_invitation").json()
    assert response["code"] == "programme_unavailable" and not response["sent"]
    attempt = rig.event.deliveries.get(message_type="loyalty_invitation")
    assert attempt.state == "deferred" and len(attempt.state) <= 16
    assert not JoinInvitation.objects.exists()
    rig.image.send_image.assert_not_called()
    assert rig.send("loyalty_invitation").json()["code"] == "accepted"
    assert rig.image.send_image.call_count == 1


@pytest.mark.parametrize(
    "changes", [{"verified_at": None}, {"provider_payment_id": ""}, {"amount": "499.00"}]
)
def test_retracted_gateway_proof_cannot_start_success_message(rig, changes):
    MorningPaymentAttempt.objects.filter(pk=rig.attempt.pk).update(**changes)
    rig.acks["core"] = 2
    assert rig.source().status_code == 409
    assert rig.send("payment_success").status_code == 409
    rig.text.send_text.assert_not_called()


def test_prior_invitation_for_another_booking_suppresses_without_leaking_its_reference(rig):
    rig.acks.update(core=2, payment_success=2)
    rig.send("payment_success")
    with morning_loyalty.registry.invitation_lock():
        decision, row = morning_loyalty.registry.claim(
            key="morning07:synthetic-other-booking",
            event_id="synthetic-other-event",
            phone=rig.stay.guest_phone,
            email=rig.stay.guest_email,
            customer_id=None,
            language="ar",
            origin="website",
            source="morning07",
            channel="whatsapp",
        )
        assert decision == "eligible"
        row.dispatched_at = timezone.now()
        row.save(update_fields=["dispatched_at"])
        morning_loyalty.registry.record(row, "accepted", "other-private-reference")
    response = rig.send("loyalty_invitation").json()
    assert response["code"] == "duplicate"
    assert response["provider_reference"] is None and response["provider_message_id"] is None
    rig.image.send_image.assert_not_called()
