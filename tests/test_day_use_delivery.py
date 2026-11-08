"""08 HTTP contracts: durable sends, scoped tokens and strict payment release."""

# ruff: noqa: F811
import hashlib
import json
from unittest.mock import Mock

import pytest
from django.core import signing
from django.urls import reverse

from apps.reservations import day_use08_gateway as gateway
from apps.reservations import day_use_auto_payment as auto
from apps.reservations import day_use_payment_messages as links
from apps.reservations.day_use08_communications import delivery_token
from apps.reservations.models import DayUsePaymentLinkDelivery
from tests.test_day_use_payments import ready as ready
from tests.test_day_use_payments import rig as rig

pytestmark = pytest.mark.django_db


def test_gateway_is_once_only_and_returns_provider_receipt_identity(ready, client, monkeypatch):
    _, s, _ = ready
    key = f"lsa08:{s.pk}:r{s.communication_revision}:status_new"
    ref = "lsa08-" + hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()
    body = dict(
        job_key=key,
        reference_id=ref,
        message_type="status_new",
        message_body="Synthetic test",
        recipient_kind="guest",
        recipient_phone=s.guest_phone.lstrip("+"),
    )
    provider = Mock()
    provider.__enter__ = Mock(return_value=provider)
    provider.__exit__ = Mock(return_value=False)
    provider.send_text.return_value = {"id": "fake-08-message"}
    monkeypatch.setattr(gateway, "UltraMsgClient", lambda: provider)
    for _ in range(2):
        response = client.post(
            reverse("integrations:day_use08_send"),
            json.dumps(body),
            content_type="application/json",
            HTTP_X_LSA_DAYUSE08_TOKEN=delivery_token(s),
        )
        assert response.status_code == 202 and response.json()["id"] == "fake-08-message"
    assert provider.send_text.call_count == 1


def test_seventh_token_does_not_authorize_eighth_gateway(ready, client):
    _, s, _ = ready
    wrong = signing.TimestampSigner(salt="reservations.morning07-delivery.v1").sign(
        f"{s.pk}:{s.communication_revision}"
    )
    assert (
        client.post(
            reverse("integrations:day_use08_send"),
            "{}",
            content_type="application/json",
            HTTP_X_LSA_DAYUSE08_TOKEN=wrong,
        ).status_code
        == 404
    )


def test_callback_boolean_is_not_receipt_proof_and_completion_is_idempotent(
    ready, client, monkeypatch
):
    _, s, _ = ready
    s.payment_link_deliveries.all().delete()
    monkeypatch.setattr(links, "configured", lambda: True)
    monkeypatch.setattr(auto, "dispatch", lambda *a: None)
    monkeypatch.setattr(auto, "core_delivered", lambda *a: False)
    for _ in range(2):
        response = client.post(
            reverse("integrations:day_use08_payment_link"),
            '{"core_delivered":true}',
            content_type="application/json",
            HTTP_X_LSA_DAYUSE08_TOKEN=delivery_token(s),
        )
        assert response.status_code == 202 and response.json()["code"] == "waiting_core"
    d = s.payment_link_deliveries.get()
    assert auto.complete(d.pk, s.communication_revision) == "core_pending"
    sender = Mock(
        side_effect=lambda pk: DayUsePaymentLinkDelivery.objects.filter(pk=pk).update(
            state="accepted"
        )
        or "accepted"
    )
    monkeypatch.setattr(links, "send_link", sender)
    monkeypatch.setattr(auto, "core_delivered", lambda *a: True)
    auto.complete(d.pk, s.communication_revision)
    auto.complete(d.pk, s.communication_revision)
    assert sender.call_count == 1
