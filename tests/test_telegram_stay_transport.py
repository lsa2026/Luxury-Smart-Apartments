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


def test_legacy_manual_resend_cannot_change_a_telegram_stay_or_create_delivery(owned):
    from uuid import uuid4

    from apps.reservations.telegram_stay_financial import modules

    scenario, stay, _ = owned
    links, domain, _ = modules(scenario)
    if scenario == "07":
        with pytest.raises(domain.MorningStayError):
            links.resend(stay.pk, uuid4(), dispatch_async=False)
    assert not stay.payment_link_deliveries.exists()


def test_due_recovery_uses_only_registered_notice_rows_not_old_events(owned, monkeypatch):
    ids = []
    monkeypatch.setattr(transport, "queue", lambda pk: ids.append(pk))
    assert transport.recover_due() == 1 and ids == [TelegramStayNotice.objects.get().pk]


def test_exhausted_source_notice_queues_one_safe_operations_email_and_no_retry(owned):
    from apps.notifications.models import EmailDelivery

    row = TelegramStayNotice.objects.get()
    row.attempts, row.last_error = 10, "snapshot_persistence_unconfirmed"
    row.save(update_fields=["attempts", "last_error"])
    assert transport.recover_due() == transport.recover_due() == 0
    delivery = EmailDelivery.objects.get(idempotency_key=f"telegram-source-exhausted:{row.pk}")
    assert delivery.recipient_source == "operations"
    assert owned[1].guest_phone not in delivery.recipient_reference
    assert owned[1].guest_email not in delivery.recipient_reference
    assert (
        EmailDelivery.objects.filter(idempotency_key=f"telegram-source-exhausted:{row.pk}").count()
        == 1
    )


def test_ycloud_source_cannot_authorize_a_telegram_owned_stay(owned, client):
    response = source_tests.request_source(client, owned, "status_new")
    assert response.status_code == 409 and response.json()["code"] == "telegram_owned_stay"


def source_cutover(settings, at):
    settings.MESSAGE_SOURCE_AUTH_ENABLED = True
    settings.MESSAGE_SOURCE_V2_ENABLED = True
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.MESSAGE_SOURCE_CUTOVER_AT = at.isoformat()


def test_v2_cutover_stops_new_telegram_registration_without_changing_existing_owner(
    owned, settings
):
    from apps.reservations import message_source_relay
    from apps.reservations.message_relay_models import WebsiteMessageRelay

    scenario, stay, event = owned
    before = list(TelegramStayChannel.objects.values())
    source_cutover(settings, event.created_at - timedelta(seconds=1))
    with transaction.atomic():
        assert transport.register(event, scenario) is True
        message_source_relay.register_event(event, scenario)
    assert list(TelegramStayChannel.objects.values()) == before
    assert TelegramStayNotice.objects.count() == 1
    assert not WebsiteMessageRelay.objects.exists()


def test_v2_new_source_is_not_stolen_by_telegram_registration_at_cutover(owned, settings):
    from apps.reservations import message_source_relay
    from apps.reservations.message_relay_models import WebsiteMessageRelay

    scenario, stay, event = owned
    TelegramStayNotice.objects.all().delete()
    TelegramStayChannel.objects.all().delete()
    source_cutover(settings, event.created_at - timedelta(seconds=1))
    with transaction.atomic():
        assert transport.register(event, scenario) is False
        message_source_relay.register_event(event, scenario)
    assert not TelegramStayChannel.objects.exists() and not TelegramStayNotice.objects.exists()
    assert WebsiteMessageRelay.objects.filter(scenario=scenario, event_id=event.pk).exists()
    assert not transport.selected(stay)


def test_future_v2_cutover_does_not_prematurely_change_telegram_ownership(owned, settings):
    scenario, stay, event = owned
    TelegramStayNotice.objects.all().delete()
    TelegramStayChannel.objects.all().delete()
    source_cutover(settings, timezone.now() + timedelta(hours=1))
    with transaction.atomic():
        assert transport.register(event, scenario) is True
    assert transport.selected(stay)
