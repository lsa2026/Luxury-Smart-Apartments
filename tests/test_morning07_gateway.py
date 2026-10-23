"""Provider/network mocks only; scenario 07 never sends live test messages."""

import hashlib
from unittest.mock import MagicMock

import pytest
from django.urls import reverse

from apps.reservations.models import Morning07DeliveryAttempt
from apps.reservations.morning07_communications import delivery_token
from tests.test_morning07_communications import paid_stay

pytestmark = pytest.mark.django_db


@pytest.fixture
def enabled(settings):
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.MORNING07_N8N_ENABLED = True
    settings.MORNING07_N8N_WEBHOOK_URL = "https://lsa25.app.n8n.cloud/webhook/lsa07-morning-stay-v1"
    settings.SITE_BASE_URL = "https://testserver"


def payload(stay, **changes):
    key = f"lsa07:{stay.pk}:r1:status"
    return {
        "job_key": key,
        "reference_id": "lsa07-" + hashlib.md5(key.encode(), usedforsecurity=False).hexdigest(),
        "message_type": "status_new",
        "recipient_kind": "guest",
        "recipient_phone": stay.guest_phone.removeprefix("+"),
        "message_body": "Synthetic confirmation",
        **changes,
    }


def gateway_mock(monkeypatch):
    factory = MagicMock()
    provider = factory.return_value.__enter__.return_value
    provider.send_text.return_value = {"sent": "true", "id": "synthetic-provider"}
    monkeypatch.setattr("apps.reservations.morning07_gateway.UltraMsgClient", factory)
    return provider


def test_gateway_requires_signed_current_payment_and_exact_recipient(enabled, client, monkeypatch):
    stay = paid_stay()
    provider = gateway_mock(monkeypatch)
    url = reverse("integrations:morning07_send")
    assert client.post(url, payload(stay), content_type="application/json").status_code == 404
    auth = {"HTTP_X_LSA_MORNING07_TOKEN": delivery_token(stay)}
    assert (
        client.post(
            url,
            payload(stay, recipient_phone="16502539999"),
            content_type="application/json",
            **auth,
        ).status_code
        == 400
    )
    assert not provider.send_text.called


def test_accepted_post_is_never_repeated_on_duplicate_request(enabled, client, monkeypatch):
    stay = paid_stay()
    provider = gateway_mock(monkeypatch)
    url = reverse("integrations:morning07_send")
    auth = {"HTTP_X_LSA_MORNING07_TOKEN": delivery_token(stay)}
    for _ in range(2):
        assert (
            client.post(url, payload(stay), content_type="application/json", **auth).status_code
            == 202
        )
    assert provider.send_text.call_count == 1
    assert Morning07DeliveryAttempt.objects.count() == 1


def test_ambiguous_post_is_reconciled_without_resending(enabled, client, monkeypatch):
    from apps.notifications.services.ultramsg import UltraMsgConnectionError

    stay = paid_stay()
    provider = gateway_mock(monkeypatch)
    provider.send_text.side_effect = UltraMsgConnectionError()
    auth = {"HTTP_X_LSA_MORNING07_TOKEN": delivery_token(stay)}
    url = reverse("integrations:morning07_send")
    for _ in range(2):
        response = client.post(url, payload(stay), content_type="application/json", **auth)
        assert response.json()["sent"] == "unknown"
    assert provider.send_text.call_count == 1


def test_receipt_filters_other_messages_and_requires_own_record(enabled, client, monkeypatch):
    stay = paid_stay()
    provider = gateway_mock(monkeypatch)
    auth = {"HTTP_X_LSA_MORNING07_TOKEN": delivery_token(stay)}
    send = reverse("integrations:morning07_send")
    client.post(send, payload(stay), content_type="application/json", **auth)
    provider.http.get.return_value.json.return_value = {
        "messages": [
            {
                "id": "other",
                "referenceId": "unrelated",
                "ack": 3,
                "body": "private unrelated content",
            },
            {"id": "synthetic-provider", "referenceId": payload(stay)["reference_id"], "ack": 2},
        ]
    }
    response = client.get(
        reverse("integrations:morning07_receipt"),
        {"reference_id": payload(stay)["reference_id"]},
        **auth,
    )
    assert response.status_code == 200
    assert len(response.json()["messages"]) == 1
    assert b"private unrelated" not in response.content
    assert (
        client.get(
            reverse("integrations:morning07_receipt"), {"reference_id": "unknown"}, **auth
        ).status_code
        == 404
    )


def test_superseded_revision_cannot_send_access_guide(enabled, client, monkeypatch):
    stay = paid_stay()
    old = delivery_token(stay)
    provider = gateway_mock(monkeypatch)
    stay.guest_name = "Updated synthetic guest"
    stay.save()
    response = client.post(
        reverse("integrations:morning07_send"),
        payload(stay),
        content_type="application/json",
        HTTP_X_LSA_MORNING07_TOKEN=old,
    )
    assert response.status_code == 404
    assert not provider.send_text.called


def test_changed_body_cannot_reuse_a_committed_reference(enabled, client, monkeypatch):
    stay = paid_stay()
    provider = gateway_mock(monkeypatch)
    auth = {"HTTP_X_LSA_MORNING07_TOKEN": delivery_token(stay)}
    url = reverse("integrations:morning07_send")
    client.post(url, payload(stay), content_type="application/json", **auth)
    response = client.post(
        url, payload(stay, message_body="Changed body"), content_type="application/json", **auth
    )
    assert response.status_code == 409
    assert provider.send_text.call_count == 1


def test_cancelled_booking_never_sends_access_instructions(enabled, client, monkeypatch):
    stay = paid_stay()
    stay.status = "cancelled"
    stay.save()
    provider = gateway_mock(monkeypatch)
    auth = {"HTTP_X_LSA_MORNING07_TOKEN": delivery_token(stay)}
    response = client.post(
        reverse("integrations:morning07_send"),
        payload(stay, message_type="checkin_guide"),
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 400
    assert not provider.send_text.called


@pytest.mark.django_db(transaction=True)
def test_concurrent_gateway_requests_cannot_repeat_provider_post(enabled, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections, connection
    from django.test import Client

    if connection.vendor != "postgresql":
        pytest.skip("Concurrent gateway claim is checked by PostgreSQL CI")
    stay = paid_stay()
    provider = gateway_mock(monkeypatch)
    started, release = Event(), Event()

    def send_once(**_kwargs):
        started.set()
        assert release.wait(10)
        return {"sent": "true", "id": "synthetic-provider"}

    provider.send_text.side_effect = send_once
    auth = {"HTTP_X_LSA_MORNING07_TOKEN": delivery_token(stay)}
    url, body = reverse("integrations:morning07_send"), payload(stay)

    def post():
        close_old_connections()
        try:
            return Client().post(url, body, content_type="application/json", **auth)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(post)
        try:
            assert started.wait(10)
            second = executor.submit(post).result(timeout=10)
            assert second.status_code == 202
            assert second.json()["sent"] == "unknown"
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 202
    assert provider.send_text.call_count == 1
