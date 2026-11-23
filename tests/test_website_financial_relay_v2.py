from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from apps.reservations import message_source_relay as relay
from apps.reservations import website_financial_source_v2 as financial
from tests import test_website_financial_source_v2 as source_tests

record, prepared = source_tests.record, source_tests.prepared
pytestmark = pytest.mark.django_db


def enable(settings):
    settings.MESSAGE_SOURCE_V2_ENABLED = True
    settings.MESSAGE_SOURCE_AUTH_ENABLED = True
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.MESSAGE_SOURCE_CUTOVER_AT = (timezone.now() - timedelta(minutes=1)).isoformat()


def test_new_source_resource_has_original_scope_and_no_financial_write(prepared, settings):
    scenario, stay, event, *_ = prepared
    enable(settings)
    with transaction.atomic():
        relay.register_payment_link(event, scenario)
        relay.register_payment_link(event, scenario)
    row = relay.WebsiteMessageRelay.objects.get(message_type="payment_link")
    delivery = stay.payment_link_deliveries.get(pk=row.event_id)
    result = financial.payment_link_source(delivery.pk, scenario)
    assert result["domain_authorized"] is True and result["dispatch_allowed"] is False
    assert row.revision == str(event.revision)
    assert row.not_before == row.source_occurred_at == delivery.created_at
    assert result["source_event_key"] == f"website:{scenario}:payment_link:{delivery.pk}"
    assert row.expires_at == stay.ends_at
    assert stay.attempts.count() == 0
    stay.refresh_from_db()
    assert stay.status == "open" and stay.paid_at is None


def test_missing_core_keeps_source_pending_without_n8n_execution(prepared, settings, monkeypatch):
    scenario, _, event, *_ = prepared
    enable(settings)
    with transaction.atomic():
        relay.register_payment_link(event, scenario)
    monkeypatch.setattr(financial, "core_delivered", lambda *_: False)
    assert relay.forward_due() == {"accepted": 0}
    row = relay.WebsiteMessageRelay.objects.get()
    assert row.state == "pending" and row.reason == "predecessor_or_domain_pending"


def test_cutover_never_registers_old_event_or_releases_old_held(prepared, settings):
    scenario, _, event, *_ = prepared
    enable(settings)
    settings.MESSAGE_SOURCE_CUTOVER_AT = (timezone.now() + timedelta(seconds=1)).isoformat()
    with transaction.atomic():
        relay.register_payment_link(event, scenario)
    assert not relay.WebsiteMessageRelay.objects.exists()


def test_existing_telegram_owner_blocks_new_source_resource(prepared, settings, monkeypatch):
    scenario, _, event, *_ = prepared
    enable(settings)
    monkeypatch.setattr("apps.reservations.telegram_stay_transport.selected", lambda _: True)
    with transaction.atomic():
        relay.register_payment_link(event, scenario)
    assert not relay.WebsiteMessageRelay.objects.exists()
