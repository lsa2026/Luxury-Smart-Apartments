"""Synthetic source outbox tests; no provider, guest, payment or live webhook."""

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core import signing
from django.db import transaction
from django.utils import timezone

from apps.reservations import message_source_authorization as source
from apps.reservations import message_source_relay as relay
from apps.reservations.message_relay_models import WebsiteMessageRelay
from tests import test_message_source_authorization as source_tests

record = source_tests.record

pytestmark = pytest.mark.django_db


@pytest.fixture
def journal(record, settings):
    scenario, stay, event = record
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.MESSAGE_SOURCE_CUTOVER_AT = (event.created_at - timedelta(seconds=1)).isoformat()
    with transaction.atomic():
        relay.register_event(event, scenario)
    return record


class Receiver:
    def __init__(self, result="bound"):
        self.result, self.calls = result, []

    def post(self, url, *, json, headers):
        assert url == relay.RELAY_URL
        assert set(headers) == {relay.READ_HEADER}
        row = WebsiteMessageRelay.objects.get(pk=json["relay_ref"])
        assert row.state == "forwarding"  # Fence before network, not after ACK.
        self.calls.append((json, headers))
        if self.result == "timeout":
            raise TimeoutError("synthetic")
        return SimpleNamespace(
            status_code=202,
            json=lambda: {
                "contract_version": "site01-source-relay-ack-1",
                "relay_ref": json["relay_ref"],
                "job_sha256": json["job_sha256"],
                "accepted": self.result == "bound",
                "status": self.result,
                "request_id": "synthetic-request-01",
            },
        )


def test_registration_is_durable_unique_and_has_no_capability_or_guest_fields(journal):
    scenario, stay, event = journal
    relay.register_event(event, scenario)
    rows = WebsiteMessageRelay.objects.all()
    assert rows.count() == 4
    serialized = json.dumps(list(rows.values()), default=str)
    assert stay.guest_phone not in serialized and stay.guest_email not in serialized
    assert "token" not in serialized and "capability" not in serialized
    reminder = rows.get(message_type="reminder_checkout")
    assert reminder.not_before == stay.ends_at - timedelta(hours=1)
    assert reminder.source_occurred_at == event.created_at


@pytest.mark.parametrize("config", ["off", "no_cutover", "naive", "future_cutover", "legacy"])
def test_registration_rejects_unconfigured_and_backlog(record, settings, config):
    scenario, _, event = record
    settings.MESSAGE_SOURCE_RELAY_ENABLED = config != "off"
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = (
        "ultramsg" if config == "legacy" else "ycloud_central"
    )
    settings.MESSAGE_SOURCE_CUTOVER_AT = {
        "no_cutover": "",
        "naive": "2026-01-01T00:00:00",
        "future_cutover": (event.created_at + timedelta(seconds=1)).isoformat(),
    }.get(config, (event.created_at - timedelta(seconds=1)).isoformat())
    relay.register_event(event, scenario)
    assert not WebsiteMessageRelay.objects.exists()


def test_due_forward_has_fresh_scoped_read_only_in_header_and_immutable_source_time(journal):
    scenario, _, event = journal
    receiver = Receiver()
    assert relay.forward_due(client=receiver, limit=3) == {"accepted": 1}
    assert len(receiver.calls) == 1
    assert receiver.calls[0][0]["source_message_type"] == "status_new"
    for envelope, headers in receiver.calls:
        assert len(envelope) == 12
        assert envelope["source_occurred_at"] == event.created_at.isoformat()
        assert type(envelope["reservation_revision"]) is str
        assert signing.TimestampSigner(salt=source.SOURCES[scenario][4].SALT).unsign(
            headers[relay.READ_HEADER], max_age=60
        ) == str(event.pk)
        assert not any("token" in k or "capability" in k or "phone" in k for k in envelope)
    assert relay.forward_due(client=receiver, limit=3) == {"accepted": 0}
    assert WebsiteMessageRelay.objects.filter(
        message_type="reminder_checkout", state="pending"
    ).exists()


@pytest.mark.parametrize("reply", ["timeout", "held", "sent"])
def test_missing_or_held_ack_never_retries_or_counts_delivery(journal, reply):
    receiver = Receiver(reply)
    assert relay.forward_due(client=receiver, limit=3) == {"accepted": 0}
    assert relay.forward_due(client=receiver, limit=3) == {"accepted": 0}
    assert len(receiver.calls) == 1
    assert (
        WebsiteMessageRelay.objects.filter(state="held" if reply == "held" else "uncertain").count()
        == 1
    )


@pytest.mark.parametrize("change", ["phone", "revision", "end", "production", "admission"])
def test_source_is_rechecked_at_due_and_changed_event_does_not_dispatch(journal, change):
    _, stay, _ = journal
    updates = {
        "phone": {"guest_phone": "+442079460000"},
        "revision": {"communication_revision": stay.communication_revision + 1},
        "end": {"ends_at": stay.ends_at + timedelta(hours=1)},
        "production": {"environment": "test"},
        "admission": {"availability_evidence": {}},
    }[change]
    type(stay).objects.filter(pk=stay.pk).update(**updates)
    receiver = Receiver()
    assert relay.forward_due(client=receiver, limit=3) == {"accepted": 0}
    assert receiver.calls == []


def test_checkout_waits_for_original_due_and_old_read_is_not_reused(journal, monkeypatch):
    scenario, stay, event = journal
    WebsiteMessageRelay.objects.exclude(message_type="reminder_checkout").delete()
    receiver = Receiver()
    assert relay.forward_due(client=receiver, limit=3) == {"accepted": 0}
    original_time = signing.time.time
    monkeypatch.setattr(signing.time, "time", lambda: original_time() - 20 * 86400)
    expired = source.SOURCES[scenario][4].event_token(event)
    monkeypatch.setattr(signing.time, "time", original_time)
    with pytest.raises(signing.SignatureExpired):
        signing.TimestampSigner(salt=source.SOURCES[scenario][4].SALT).unsign(
            expired, max_age=15 * 86400
        )
    monkeypatch.setattr(timezone, "now", lambda: stay.ends_at - timedelta(minutes=50))
    assert relay.forward_due(client=receiver, limit=3) == {"accepted": 1}
    assert receiver.calls[0][1][relay.READ_HEADER] != expired


def test_legacy_outbox_is_closed_in_central_mode(journal):
    scenario, stay, _ = journal
    comms, payments = source.SOURCES[scenario][4:6]
    receiver = Receiver()
    assert comms.deliver_pending_events(client=receiver, stay_id=stay.pk) == {"accepted": 0}
    assert payments.deliver_event("not-a-real-event", client=receiver) == {"accepted": False}
    assert receiver.calls == []


def test_financial_due_and_original_payment_time_are_distinct(record, settings):
    scenario, stay, event = source_tests.paid_record(record)
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.MESSAGE_SOURCE_CUTOVER_AT = (stay.paid_at - timedelta(seconds=1)).isoformat()
    relay.register_event(event, scenario, financial=True)
    row = WebsiteMessageRelay.objects.get(message_type="payment_success")
    assert row.source_occurred_at == stay.paid_at
    assert row.not_before == event.created_at
    with transaction.atomic():
        current = relay.current_event(row)
    assert current is not None
    assert current[1]["domain_authorized"] is False


def test_blocked_predecessors_do_not_starve_another_current_booking(journal, monkeypatch):
    scenario, stay, event = journal
    WebsiteMessageRelay.objects.all().delete()
    rows = []
    for i in range(31):
        rows.append(
            WebsiteMessageRelay.objects.create(
                scenario=scenario,
                event_id=uuid4(),
                stay_id=stay.pk,
                revision=str(stay.communication_revision),
                message_type="status_new",
                job_sha256=f"{i:064x}",
                source_occurred_at=event.created_at,
                not_before=event.created_at,
                expires_at=stay.ends_at,
            )
        )
    from apps.reservations import message_receipts

    monkeypatch.setattr(message_receipts, "core_slot_delivered", lambda *a: False)
    monkeypatch.setattr(
        relay,
        "current_event",
        lambda row: (
            SimpleNamespace(pk=row.event_id, stay=stay),
            {
                "source_event_key": f"website:{scenario}:core:{row.event_id}",
                "domain_authorized": row.pk == rows[-1].pk,
            },
        ),
    )
    receiver = Receiver()
    assert relay.forward_due(client=receiver) == {"accepted": 0}
    assert receiver.calls == []
    assert relay.forward_due(client=receiver) == {"accepted": 1}
    assert receiver.calls[0][0]["relay_ref"] == str(rows[-1].pk)
    assert WebsiteMessageRelay.objects.filter(not_before=event.created_at).count() == 31
