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


def deferred_client():
    from types import SimpleNamespace

    calls = []

    def post(url, *, json, headers):
        calls.append(json)
        reply = {
            "contract_version": "site01-source-relay-ack-2",
            "relay_ref": json["relay_ref"],
            "job_sha256": json["job_sha256"],
            "accepted": False,
            "status": "deferred",
            "reason": "waiting_predecessor",
            "request_id": None,
            "attempts_claimed": 0,
            "provider_message_id": None,
            "wamid": None,
            "retryable_before_claim": True,
        }
        return SimpleNamespace(status_code=200, json=lambda: reply)

    return SimpleNamespace(post=post), calls


def test_explicit_preclaim_deferral_waits_in_source_without_retiming_or_n8n_polling(
    prepared, settings
):
    scenario, _, event, *_ = prepared
    enable(settings)
    with transaction.atomic():
        relay.register_payment_link(event, scenario)
    row = relay.WebsiteMessageRelay.objects.get()
    original_times = (row.not_before, row.source_occurred_at, row.expires_at)
    client, calls = deferred_client()
    assert relay.forward_due(client=client) == {"accepted": 0}
    row.refresh_from_db()
    assert row.state == "pending" and row.reason == "deferred_before_claim:waiting_predecessor"
    assert (row.not_before, row.source_occurred_at, row.expires_at) == original_times
    assert calls[0]["contract_version"] == "site01-source-relay-2"
    assert calls[0]["source_kind"] == "payment_link"
    assert relay.forward_due(client=client) == {"accepted": 0}
    assert len(calls) == 1
    type(row).objects.filter(pk=row.pk).update(updated_at=timezone.now() - timedelta(minutes=16))
    assert relay.forward_due(client=client) == {"accepted": 0}
    assert len(calls) == 2 and calls[1]["relay_ref"] == calls[0]["relay_ref"]
    row.refresh_from_db()
    assert (row.not_before, row.source_occurred_at, row.expires_at) == original_times


@pytest.mark.parametrize(
    "change", ["claim", "rid", "provider", "reason", "retry", "extra", "timeout"]
)
def test_ambiguous_or_postclaim_reply_never_retries(prepared, settings, change):
    scenario, _, event, *_ = prepared
    enable(settings)
    with transaction.atomic():
        relay.register_payment_link(event, scenario)
    client, calls = deferred_client()
    original_post = client.post

    def post(*args, **kwargs):
        response = original_post(*args, **kwargs)
        if change == "timeout":
            raise TimeoutError
        body = response.json()
        mutations = {
            "claim": {"attempts_claimed": 1},
            "rid": {"request_id": "already-bound"},
            "provider": {"provider_message_id": "attempted"},
            "reason": {"reason": "unknown"},
            "retry": {"retryable_before_claim": False},
            "extra": {"caller_approved": True},
        }
        body.update(mutations[change])
        return response

    client.post = post
    assert relay.forward_due(client=client) == {"accepted": 0}
    row = relay.WebsiteMessageRelay.objects.get()
    assert row.state == "uncertain"
    assert relay.forward_due(client=client) == {"accepted": 0}
    assert len(calls) == 1
