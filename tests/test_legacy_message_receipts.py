"""Historical exact receipts keep existing checkout eligible without a new send."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.reservations import guest_message_transport as transport
from apps.reservations import legacy_message_receipts as legacy
from apps.reservations import message_receipts as central
from apps.reservations import message_source_authorization as source
from tests import test_message_source_authorization as source_tests

record = source_tests.record
pytestmark = pytest.mark.django_db


@pytest.fixture
def old_delivery(record, settings, monkeypatch):
    scenario, stay, event = record
    from apps.reservations import day_use08_gateway, morning07_gateway

    gateway = morning07_gateway if scenario == "07" else day_use08_gateway
    model = source.SOURCES[scenario][3]
    keys = source.core_keys(stay.pk, event.payload, scenario)
    settings.MESSAGE_SOURCE_CUTOVER_AT = (timezone.now() - timedelta(minutes=1)).isoformat()
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.ULTRAMSG_INSTANCE_ID = "synthetic-legacy-instance"
    rows = {}
    for kind, key in keys.items():
        if kind == "reminder_checkout":
            continue
        row = model.objects.create(
            reference_id=source.legacy_reference(key, scenario),
            stay=stay,
            revision=stay.communication_revision,
            payload_hash="a" * 64,
            state="accepted",
            provider_id="synthetic-" + kind,
        )
        model.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(minutes=2))
        row.refresh_from_db()
        rows[kind] = row
    factory = Mock()
    provider = factory.return_value.__enter__.return_value
    provider.http.get.return_value.json.return_value = [
        {"id": row.provider_id, "referenceId": row.pk, "ack": 2, "status": "delivered"}
        for row in rows.values()
    ]
    monkeypatch.setattr(gateway, "UltraMsgClient", factory)
    return scenario, stay, event, gateway, rows, factory


def test_authenticated_old_provider_read_is_cached_bound_and_does_not_send(old_delivery):
    scenario, stay, _, gateway, rows, factory = old_delivery
    before = (stay.status, stay.amount_sar, stay.paid_at)
    assert central.core_delivered(stay, stay.communication_revision, scenario)
    assert factory.return_value.__enter__.return_value.http.get.call_count == 3
    for row in rows.values():
        row.refresh_from_db()
        assert legacy.cached_rows(row) and row.state == "accepted"
    factory.reset_mock()
    factory.side_effect = AssertionError("provider must not be queried after verified cache")
    assert central.core_delivered(stay, stay.communication_revision, scenario)
    assert (stay.status, stay.amount_sar, stay.paid_at) == before
    factory.assert_not_called()


def test_existing_day_use_payment_gate_remains_open_from_exact_old_delivery(old_delivery, settings):
    scenario, stay, _, _, _, factory = old_delivery
    if scenario != "08":
        pytest.skip("Only08 has the guest checkout core-delivery dependency")
    from apps.payments.day_use_payments import require_payable
    from apps.reservations.day_use_payment_messages import fingerprint
    from apps.reservations.models import DayUsePaymentLinkDelivery

    settings.HYPERPAY_ENABLED = True
    settings.HYPERPAY_ENVIRONMENT = "production"
    DayUsePaymentLinkDelivery.objects.create(
        stay=stay, fingerprint=fingerprint(stay), state="accepted"
    )
    require_payable(stay)
    provider = factory.return_value.__enter__.return_value
    provider.send_text.assert_not_called()
    provider.send_image.assert_not_called()


def test_source_sequence_uses_only_cached_delivery_and_never_provider_io(old_delivery):
    scenario, stay, event, gateway, rows, factory = old_delivery
    rules = source.sha(source.core_keys(stay.pk, event.payload, scenario)["house_rules"])
    guide = source.sha(source.core_keys(stay.pk, event.payload, scenario)["checkin_guide"])
    assert source.core_facts(stay, event, scenario, rules)["domain_authorized"] is False
    assert source.core_facts(stay, event, scenario, guide)["domain_authorized"] is False
    factory.assert_not_called()
    gateway.exact_receipt_messages(rows["status_new"])
    factory.reset_mock()
    assert source.core_facts(stay, event, scenario, rules)["domain_authorized"] is True
    assert source.core_facts(stay, event, scenario, guide)["domain_authorized"] is False
    factory.assert_not_called()
    gateway.exact_receipt_messages(rows["house_rules"])
    factory.reset_mock()
    assert source.core_facts(stay, event, scenario, guide)["domain_authorized"] is True
    factory.assert_not_called()


@pytest.mark.parametrize(
    "mutation", ["no_cutover", "future", "new_attempt", "detached", "provider_changed"]
)
def test_central_reader_cannot_query_new_or_unbound_legacy_attempt(
    old_delivery, settings, mutation
):
    _, _, _, gateway, rows, factory = old_delivery
    row = rows["status_new"]
    if mutation == "no_cutover":
        settings.MESSAGE_SOURCE_CUTOVER_AT = ""
    elif mutation == "future":
        settings.MESSAGE_SOURCE_CUTOVER_AT = (timezone.now() + timedelta(days=1)).isoformat()
    elif mutation == "new_attempt":
        type(row).objects.filter(pk=row.pk).update(created_at=timezone.now())
        row.refresh_from_db()
    elif mutation == "detached":
        row = SimpleNamespace(provider_id=row.provider_id, reference_id=row.pk)
    else:
        row.provider_id = "not-stored-provider-id"
    with pytest.raises(ValueError, match="receipt_lookup_uncertain"):
        gateway.exact_receipt_messages(row)
    factory.assert_not_called()


@pytest.mark.parametrize("ack", [0, 1, "sent", "accepted"])
def test_acceptance_or_sent_never_creates_delivery_evidence(old_delivery, ack):
    _, _, _, gateway, rows, factory = old_delivery
    row = rows["status_new"]
    factory.return_value.__enter__.return_value.http.get.return_value.json.return_value = [
        {"id": row.provider_id, "referenceId": row.pk, "ack": ack, "status": "sent"}
    ]
    gateway.exact_receipt_messages(row)
    row.refresh_from_db()
    assert legacy.cached_rows(row) == [] and legacy.KEY not in row.response


@pytest.mark.parametrize("mutation", ["edited_ack", "copied", "identity", "unmaced"])
def test_response_json_is_not_trusted_without_exact_site_proof(old_delivery, mutation):
    _, _, _, gateway, rows, _ = old_delivery
    row = rows["status_new"]
    gateway.exact_receipt_messages(row)
    row.refresh_from_db()
    if mutation == "edited_ack":
        row.response[legacy.KEY]["row"]["ack"] = "4"
    elif mutation == "copied":
        other = rows["house_rules"]
        other.response = deepcopy(row.response)
        row = other
    elif mutation == "identity":
        row.payload_hash = "b" * 64
    else:
        row.response[legacy.KEY].pop("proof_mac")
    assert legacy.cached_rows(row) == []


def test_legacy_reader_does_not_allow_a_legacy_send(old_delivery):
    _, stay, _, _, rows, factory = old_delivery
    transport.require_legacy_receipt(rows["status_new"])
    with pytest.raises(transport.TransportHeld):
        transport.prepare(stay, source="morning07", purpose="status_new", reference="new-send")
    factory.assert_not_called()
