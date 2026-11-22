"""Permanent channel choice and notification durability, synthetic records only."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import transaction
from django.utils import timezone

from apps.reservations import guest_message_transport as legacy
from apps.reservations import telegram_stay_transport as transport
from apps.reservations.models import DayUseHostawayOwnership
from apps.reservations.telegram_stay_models import (
    TelegramStayChannel,
    TelegramStayMessage,
    TelegramStayNotice,
)
from tests import test_message_source_authorization as source_tests

pytestmark = pytest.mark.django_db
record = source_tests.record


@pytest.fixture
def owned(record, settings):
    scenario, stay, event = record
    settings.TELEGRAM_STAY_MESSAGES_ENABLED = True
    settings.TELEGRAM_STAY_CUTOVER_AT = (stay.created_at - timedelta(seconds=1)).isoformat()
    if scenario == "08":
        DayUseHostawayOwnership.objects.create(
            stay=stay,
            revision=2,
            acknowledged_revision=2,
            state="confirmed",
            provider_reservation_id=event.payload["hostaway_reservation_id"],
        )
    with transaction.atomic():
        assert transport.register(event, scenario)
    return record


def test_capture_selection_is_unique_and_never_stores_phone_body_or_capability(owned):
    scenario, stay, event = owned
    with transaction.atomic():
        assert transport.register(event, scenario)
    assert TelegramStayChannel.objects.count() == TelegramStayNotice.objects.count() == 1
    data = str(list(TelegramStayChannel.objects.values())) + str(
        list(TelegramStayNotice.objects.values())
    )
    assert (
        stay.guest_phone not in data and stay.guest_email not in data and "capability" not in data
    )


@pytest.mark.parametrize("case", ["off", "empty", "naive", "future", "old_booking"])
def test_no_channel_selection_or_historical_backfill_without_explicit_current_cutover(
    record, settings, case
):
    scenario, stay, event = record
    settings.TELEGRAM_STAY_MESSAGES_ENABLED = case != "off"
    settings.TELEGRAM_STAY_CUTOVER_AT = {
        "empty": "",
        "naive": "2026-11-22T12:00:00",
        "future": (timezone.now() + timedelta(minutes=1)).isoformat(),
        "old_booking": (stay.created_at + timedelta(microseconds=1)).isoformat(),
    }.get(case, (stay.created_at - timedelta(seconds=1)).isoformat())
    with transaction.atomic():
        assert not transport.register(event, scenario)
    assert not TelegramStayChannel.objects.exists() and not TelegramStayNotice.objects.exists()


class Receiver:
    def __init__(self, owned, change=None):
        self.owned, self.change, self.calls = owned, change, 0

    def post(self, url, *, json):
        scenario, stay, event = self.owned
        row = TelegramStayNotice.objects.get()
        assert row.attempts == 1 and row.lease_until > timezone.now()
        assert url == transport.URLS[scenario]
        assert json == {
            "contract": "tg39_stay_notification_v1",
            "scenario": scenario,
            "event_id": str(event.pk),
        }
        self.calls += 1
        reply = {
            "contract": "tg39_stay_event_ack_v1",
            "scenario": scenario,
            "event_id": str(event.pk),
            "stay_key": event.payload["reservation_key"],
            "source_revision": stay.communication_revision,
            "persisted": True,
            "channel": "telegram",
        }
        if self.change == "revision":
            reply["source_revision"] = str(stay.communication_revision)
        elif self.change == "accepted":
            reply["accepted"] = True
        elif self.change == "channel":
            reply["channel"] = "whatsapp"
        return SimpleNamespace(status_code=200, json=lambda: reply)


def test_snapshot_ack_is_persistence_only_and_not_core_message_acceptance(owned):
    receiver = Receiver(owned)
    row = TelegramStayNotice.objects.get()
    assert transport.deliver(row.pk, client=receiver) == "persisted"
    assert transport.deliver(row.pk, client=receiver) == "persisted" and receiver.calls == 1
    assert not TelegramStayMessage.objects.exists()


@pytest.mark.parametrize("case", ["revision", "accepted", "channel"])
def test_false_or_mismatched_ack_keeps_notice_pending_without_provider_claim(owned, case):
    row = TelegramStayNotice.objects.get()
    assert transport.deliver(row.pk, client=Receiver(owned, case)) == "pending"
    row.refresh_from_db()
    assert row.state == "pending" and row.attempts == 1 and row.last_error
    assert not TelegramStayMessage.objects.exists()


def test_selected_stay_never_reopens_legacy_send_when_new_transport_is_paused(owned, settings):
    scenario, stay, _ = owned
    settings.TELEGRAM_STAY_MESSAGES_ENABLED = False
    assert transport.selected(stay)
    with pytest.raises(legacy.TransportHeld, match="telegram_owned_stay"):
        legacy.prepare(
            stay,
            source="morning07" if scenario == "07" else "day_use08",
            purpose="house_rules",
            reference="synthetic",
        )
    route = legacy.Route(
        "morning07" if scenario == "07" else "day_use08",
        str(stay.pk),
        1,
        "house_rules",
        "synthetic",
        "ultramsg",
        verify_telegram_owner=True,
    )
    with pytest.raises(legacy.TransportHeld, match="telegram_owned_stay"):
        route.open(lambda: pytest.fail("No provider constructed"))


def test_due_recovery_uses_only_registered_notice_rows_not_old_events(owned, monkeypatch):
    ids = []
    monkeypatch.setattr(transport, "queue", lambda pk: ids.append(pk))
    assert transport.recover_due() == 1 and ids == [TelegramStayNotice.objects.get().pk]


def test_ycloud_source_cannot_authorize_a_telegram_owned_stay(owned, client):
    response = source_tests.request_source(client, owned, "status_new")
    assert response.status_code == 409 and response.json()["code"] == "telegram_owned_stay"
