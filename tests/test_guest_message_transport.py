"""Closed YCloud seam; all booking/payment facts are disposable test fixtures."""
# ruff: noqa: F811

import hashlib
import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.loyalty.models import JoinInvitation
from apps.reservations import guest_message_transport as transport
from tests.day_use08_payment_harness import PaymentHarness as DayHarness
from tests.morning07_payment_harness import PaymentHarness as MorningHarness
from tests.test_morning_hostaway import flags as flags  # noqa: F401
from tests.test_morning_stays import isolated as isolated  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def enabled(settings, isolated, flags):
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.MORNING07_N8N_ENABLED = True
    settings.MORNING07_PAYMENT_HANDOFF_ENABLED = True
    settings.DAY_USE08_PAYMENT_HANDOFF_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ultramsg"
    settings.MORNING07_PAYMENT_WEBHOOK_URL = "https://example.test/07"
    settings.DAY_USE08_PAYMENT_WEBHOOK_URL = "https://example.test/08"


@pytest.fixture(params=["07", "08"])
def rig(request):
    code = request.param
    factory = MorningHarness if code == "07" else DayHarness
    prefix = "morning" if code == "07" else "day_use"
    return SimpleNamespace(
        code=code,
        factory=factory,
        source="morning07" if code == "07" else "day_use08",
        gateway=importlib.import_module(f"apps.reservations.{prefix}{code}_gateway"),
        financial=importlib.import_module(f"apps.reservations.{prefix}{code}_payments"),
        links=importlib.import_module(f"apps.reservations.{prefix}_payment_messages"),
        loyalty=importlib.import_module(f"apps.reservations.{prefix}_loyalty"),
    )


def job(h, code, **changes):
    key = f"lsa{code}:{h.stay.pk}:r{h.stay.communication_revision}:status"
    return {
        "job_key": key,
        "reference_id": f"lsa{code}-"
        + hashlib.md5(key.encode(), usedforsecurity=False).hexdigest(),
        "message_type": "status_new",
        "recipient_kind": "guest",
        "recipient_phone": h.stay.guest_phone.lstrip("+"),
        "message_body": "Synthetic confirmation; no template approval inferred",
        **changes,
    }


def test_central_core_holds_before_claim_or_legacy_provider(rig, settings):
    with rig.factory() as h:
        before = rig.gateway.__dict__[
            "Morning07DeliveryAttempt" if rig.code == "07" else "DayUse08DeliveryAttempt"
        ].objects.count()
        settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
        for _ in range(2):
            response = h.phase({"action": "booking_send", "job": job(h, rig.code)})
            assert response["status"] == 503
            assert response["body"]["error"] == "ycloud_contract_pending"
        model = rig.gateway.__dict__[
            "Morning07DeliveryAttempt" if rig.code == "07" else "DayUse08DeliveryAttempt"
        ]
        assert model.objects.count() == before
        h.core_client.assert_not_called()


def test_domain_recipient_validation_precedes_transport_hold(rig, settings):
    with rig.factory() as h:
        settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
        response = h.phase(
            {"action": "booking_send", "job": job(h, rig.code, recipient_phone="12025550999")}
        )
        assert response["status"] == 400
        h.core_client.assert_not_called()


def test_central_payment_success_and_loyalty_do_not_consume_claim(rig, settings):
    with rig.factory() as h:
        h.acks["core"] = 2
        settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
        assert h.send("payment_success").json()["code"] == "ycloud_contract_pending"
        assert not h.event.deliveries.exists()
        h.text_client.assert_not_called()
        # Create only a synthetic notice proof through the unchanged legacy mock.
        settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ultramsg"
        assert h.send("payment_success").json()["code"] == "accepted"
        h.acks["payment_success"] = 2
        settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
        assert h.send("loyalty_invitation").json()["code"] == "ycloud_contract_pending"
        assert h.event.deliveries.count() == 1
        assert not JoinInvitation.objects.exists()
        h.image_client.assert_not_called()


def test_stale_financial_proof_precedes_transport_and_never_confirms_payment(rig, settings):
    with rig.factory() as h:
        h.attempt.status = "pending"
        h.attempt.save(update_fields=["status"])
        settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
        result = h.send("payment_success")
        assert result.status_code == 409 and result.json()["code"] == "stale"
        assert not h.event.deliveries.exists()
        h.text_client.assert_not_called()


def test_direct_loyalty_transport_holds_before_registry_consume(rig, settings):
    with rig.factory() as h:
        h.acks["core"] = 2
        assert h.send("payment_success").json()["code"] == "accepted"
        h.acks["payment_success"] = 2
        _, ref = rig.financial.reference(h.event.pk, "loyalty_invitation")
        h.event.deliveries.create(reference_id=ref, message_type="loyalty_invitation")
        settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
        response = rig.loyalty.invite_paid_booking(
            h.stay,
            h.stay.communication_revision,
            payment_event_id=h.event.pk,
            payment_delivery_id=ref,
        )
        assert json.loads(response.content)["code"] == "ycloud_contract_pending"
        assert not JoinInvitation.objects.exists()
        h.image_client.assert_not_called()


def test_payment_link_transport_holds_without_mutating_booking_or_delivery(rig, settings):
    with rig.factory(full_journey=True) as h:
        fixtures = importlib.import_module(
            "tests.test_morning_auto_payment" if rig.code == "07" else "tests.day_use08_fixtures"
        )
        fixtures.receipts(h.stay)
        h.acks["core"] = 2
        model = rig.links.__dict__[
            "MorningPaymentLinkDelivery" if rig.code == "07" else "DayUsePaymentLinkDelivery"
        ]
        row = model.objects.create(stay=h.stay, fingerprint=rig.links.fingerprint(h.stay))
        before = (h.stay.status, h.stay.amount_sar, h.stay.paid_at)
        settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
        assert rig.links.send_link(row.pk) == "transport_held"
        row.refresh_from_db()
        h.stay.refresh_from_db()
        assert row.state == "queued" and row.provider_id == ""
        assert (h.stay.status, h.stay.amount_sar, h.stay.paid_at) == before
        h.link_client.assert_not_called()


def test_central_receipts_never_query_or_translate_ultramsg_ack(rig, settings, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(rig.gateway, "UltraMsgClient", factory)
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    with pytest.raises(ValueError, match="receipt_lookup_uncertain"):
        rig.gateway.exact_receipt_messages(
            SimpleNamespace(provider_id="synthetic", reference_id="r")
        )
    factory.assert_not_called()


@pytest.mark.parametrize("selected", ["ycloud_central", "invalid", "", "ultramsg "])
def test_invalid_or_pending_transport_cannot_fall_back(selected, settings):
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = selected
    stay = SimpleNamespace(pk="synthetic", communication_revision=1)
    with pytest.raises(transport.TransportHeld):
        transport.prepare(stay, source="morning07", purpose="status_new", reference="synthetic")


def test_workgroup_is_not_passed_to_ycloud(settings):
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    with pytest.raises(transport.TransportHeld, match="workgroup_transport_not_supported"):
        transport.prepare(
            None, source="morning07", purpose="group_notice", reference="r", recipient_kind="group"
        )


def test_transport_changed_after_preparation_does_not_construct_legacy_client(settings):
    stay = SimpleNamespace(pk="synthetic", communication_revision=1)
    route = transport.prepare(stay, source="morning07", purpose="status_new", reference="r")
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    provider = Mock()
    with pytest.raises(transport.TransportHeld):
        route.open(provider)
    provider.assert_not_called()
