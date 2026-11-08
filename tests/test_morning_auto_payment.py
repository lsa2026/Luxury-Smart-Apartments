"""Synthetic delivery receipts and mocked providers; never send a live payment link."""

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse

from apps.reservations import morning_auto_payment as service
from apps.reservations import morning_payment_messages as links
from apps.reservations.models import (
    Morning07DeliveryAttempt,
    MorningPaymentLinkDelivery,
    MorningStay,
)
from apps.reservations.morning07_communications import delivery_token
from apps.reservations.morning_hostaway import booking_state, record_in_hostaway
from tests.test_morning_hostaway import flags as flags  # noqa: F401
from tests.test_morning_hostaway import request_for
from tests.test_morning_hostaway_first import unpaid
from tests.test_morning_stays import isolated as isolated  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def enabled(settings, isolated):  # noqa: F811
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.MORNING07_N8N_ENABLED = True
    settings.ULTRAMSG_ENABLED = True
    settings.ULTRAMSG_INSTANCE_ID = "synthetic"
    settings.ULTRAMSG_TOKEN = "synthetic"
    with (
        patch("apps.reservations.morning07_communications.queue_stay_event"),
        patch.object(service, "dispatch"),
    ):
        yield


def confirmed():
    stay, remote, _ = unpaid()
    record_in_hostaway(request=request_for(stay), stay_id=stay.pk, client=remote)
    stay.refresh_from_db()
    return stay, remote


def post(stay, data=None, token=None):
    return Client().post(
        reverse("integrations:morning07_payment_link"),
        {"core_delivered": True} if data is None else data,
        content_type="application/json",
        HTTP_X_LSA_MORNING07_TOKEN=delivery_token(stay) if token is None else token,
    )


def intent(stay):
    assert post(stay).status_code == 202
    return stay.payment_link_deliveries.get()


def receipts(stay, count=3):
    phone = stay.guest_phone.removeprefix("+")
    stamp = int(stay.starts_at.timestamp() * 1000)
    guide = f"guide:{stay.property.hostaway_listing_map_id}:{stamp}:{phone}"
    for suffix in [f"r{stay.communication_revision}:status", "rules:" + phone, guide][:count]:
        reference = (
            "lsa07-"
            + hashlib.md5(f"lsa07:{stay.pk}:{suffix}".encode(), usedforsecurity=False).hexdigest()
        )
        Morning07DeliveryAttempt.objects.get_or_create(
            reference_id=reference,
            defaults={
                "stay": stay,
                "revision": stay.communication_revision,
                "payload_hash": "a" * 64,
                "state": "accepted",
                "provider_id": "synthetic-" + reference,
            },
        )


def test_callback_requires_current_signed_reference_and_boolean_claim():
    stay, _ = confirmed()
    assert post(stay, token="invalid").status_code == 404
    assert post(stay, {"core_delivered": "true"}).status_code == 400
    assert post(stay, {"core_delivered": True, "recipient": "arbitrary"}).status_code == 400
    assert post(stay, {"core_delivered": False}).json()["code"] == "core_pending"
    assert not MorningPaymentLinkDelivery.objects.exists()
    assert Client().get(reverse("integrations:morning07_payment_link")).status_code == 405


def test_duplicate_callbacks_create_one_durable_intent_and_dispatch_after_commit(
    django_capture_on_commit_callbacks,
):
    stay, _ = confirmed()
    with patch.object(service, "dispatch") as dispatch:
        with django_capture_on_commit_callbacks(execute=True):
            assert post(stay).json()["code"] == "waiting_core"
            assert post(stay).json()["code"] == "waiting_core"
            dispatch.assert_not_called()
        dispatch.assert_called_once()
    assert stay.payment_link_deliveries.count() == 1


def test_two_messages_or_provider_acceptance_alone_never_send_payment_link():
    stay, _ = confirmed()
    delivery = intent(stay)
    receipts(stay, count=2)
    with (
        patch(
            "apps.reservations.morning_loyalty.exact_receipt_messages", return_value=[{"ack": 2}]
        ),
        patch.object(links, "send_link") as send,
    ):
        assert service.complete(delivery.pk, stay.communication_revision) == "core_pending"
        send.assert_not_called()
    receipts(stay)
    with (
        patch(
            "apps.reservations.morning_loyalty.exact_receipt_messages", return_value=[{"ack": 1}]
        ),
        patch.object(links, "send_link") as send,
    ):
        assert service.complete(delivery.pk, stay.communication_revision) == "core_pending"
        send.assert_not_called()


def test_third_delivered_receipt_sends_one_real_implementation_link_and_duplicates_do_not_resend():
    stay, remote = confirmed()
    delivery = intent(stay)
    receipts(stay)
    own = booking_state(stay)
    remote.guest_stay_page.return_value = [
        {
            "id": own["reservation_id"],
            "listingMapId": stay.property.hostaway_listing_map_id,
            "source": "LSA_MorningStay",
            "status": "new",
            "arrivalDate": own["arrival_date"],
            "departureDate": own["departure_date"],
        }
    ]
    with (
        patch(
            "apps.reservations.morning_loyalty.exact_receipt_messages", return_value=[{"ack": 2}]
        ) as proof,
        patch("apps.reservations.morning_stays.HostawayClient") as hostaway,
        patch.object(links, "UltraMsgClient") as provider,
    ):
        hostaway.return_value.__enter__.return_value = remote
        client = provider.return_value.__enter__.return_value
        client.send_image.return_value = {"id": "synthetic-payment-link", "sent": "true"}
        assert service.complete(delivery.pk, stay.communication_revision) == "accepted"
        assert proof.call_count == 3
        assert service.complete(delivery.pk, stay.communication_revision) == "accepted"
        assert post(stay).json()["code"] == "accepted"
        assert client.send_image.call_count == 1
        assert client.send_image.call_args.kwargs["caption"].count(links.public_url(stay)) == 1


@pytest.mark.parametrize("state", ["paid", "cancelled", "processing", "review"])
def test_state_change_before_completion_prevents_link(state):
    stay, _ = confirmed()
    delivery = intent(stay)
    MorningStay.objects.filter(pk=stay.pk).update(status=state)
    with patch.object(links, "send_link") as send:
        assert service.complete(delivery.pk, stay.communication_revision) == "expired"
        send.assert_not_called()


@pytest.mark.parametrize(
    "state", ["queued", "sending", "uncertain", "accepted", "delivered", "read"]
)
def test_prior_manual_link_suppresses_automatic_send(state):
    stay, _ = confirmed()
    prior = MorningPaymentLinkDelivery.objects.create(
        pk=uuid.uuid4(),
        stay=stay,
        state=state,
        fingerprint=links.fingerprint(stay),
    )
    assert post(stay).json()["code"] == state
    assert stay.payment_link_deliveries.get().pk == prior.pk


def test_manual_send_while_core_pending_wins_without_automatic_duplicate():
    stay, _ = confirmed()
    delivery = intent(stay)
    MorningPaymentLinkDelivery.objects.create(
        stay=stay,
        state="accepted",
        fingerprint=links.fingerprint(stay),
    )
    with (
        patch.object(service, "core_delivered", return_value=True),
        patch.object(links, "send_link") as send,
    ):
        assert service.complete(delivery.pk, stay.communication_revision) == "expired"
        send.assert_not_called()


def test_uncertain_provider_post_is_never_repeated_automatically():
    from apps.notifications.services.ultramsg import UltraMsgConnectionError

    stay, _ = confirmed()
    delivery = intent(stay)
    with (
        patch.object(service, "core_delivered", return_value=True),
        patch.object(links, "verify_window", return_value={}),
        patch.object(links, "UltraMsgClient") as provider,
    ):
        client = provider.return_value.__enter__.return_value
        client.send_image.side_effect = UltraMsgConnectionError()
        assert service.complete(delivery.pk, stay.communication_revision) == "uncertain"
        assert service.complete(delivery.pk, stay.communication_revision) == "uncertain"
        assert post(stay).json()["code"] == "uncertain"
        assert client.send_image.call_count == 1


def test_receipt_uncertainty_waits_and_retry_budget_closes_without_sending():
    from apps.reservations.tasks import complete_morning_payment_link

    stay, _ = confirmed()
    delivery = intent(stay)
    with patch.object(service, "core_delivered", side_effect=ValueError):
        assert service.complete(delivery.pk, stay.communication_revision) == "core_pending"
    complete_morning_payment_link.push_request(retries=10)
    try:
        with patch.object(service, "complete", return_value="core_pending"):
            assert (
                complete_morning_payment_link.run(delivery.pk, stay.communication_revision)
                == "expired"
            )
    finally:
        complete_morning_payment_link.pop_request()
    delivery.refresh_from_db()
    assert delivery.state == "expired" and delivery.last_error == "core_delivery_not_verified"


@pytest.mark.django_db(transaction=True)
def test_parallel_completion_claims_only_one_provider_send():
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock regression requires PostgreSQL.")
    stay, _ = confirmed()
    delivery = intent(stay)
    barrier = Barrier(2)

    def proof(*args):
        barrier.wait(timeout=10)
        return True

    def worker():
        close_old_connections()
        try:
            return service.complete(delivery.pk, stay.communication_revision)
        finally:
            close_old_connections()

    with (
        patch.object(service, "core_delivered", side_effect=proof),
        patch.object(links, "verify_window", return_value={}),
        patch.object(links, "UltraMsgClient") as provider,
    ):
        client = provider.return_value.__enter__.return_value
        client.send_image.return_value = {"id": "synthetic-parallel-link", "sent": "true"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        assert "accepted" in results
        assert client.send_image.call_count == 1
