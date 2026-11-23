"""Isolated source/domain checks, no provider, real booking or payment."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.reservations import website_financial_source_v2 as financial
from tests import test_message_source_authorization as originals

record = originals.record
pytestmark = pytest.mark.django_db


@pytest.fixture
def prepared(record, settings, monkeypatch):
    scenario, stay, event = record
    settings.HYPERPAY_ENABLED = True
    settings.HYPERPAY_ENVIRONMENT = "production"
    type(stay.property).objects.filter(pk=stay.property_id).update(
        name_ar="شقة مصطنعة", name_en="Synthetic Apartment", name_fr="Appartement fictif"
    )
    stay.property.refresh_from_db()
    with transaction.atomic():
        originals.source.SOURCES[scenario][4].capture_event(stay.pk)
    stay.refresh_from_db()
    event = stay.latest_communication_event()
    links, domain = financial.modules(scenario)
    row = stay.payment_link_deliveries.create(fingerprint=links.fingerprint(stay))
    calls = []
    monkeypatch.setattr(financial, "core_delivered", lambda *_: True)
    monkeypatch.setattr(domain, "validate_window", lambda *a, **k: None)
    monkeypatch.setattr(domain, "verify_window", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(domain, "local_inflight_conflict", lambda *a: False)
    return scenario, stay, event, row, calls


def read(prepared):
    return financial.payment_link_source(prepared[3].pk, prepared[0])


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_single_current_source_locks_and_rechecks_without_business_writes(prepared, language):
    scenario, stay, _, row, calls = prepared
    type(stay).objects.filter(pk=stay.pk).update(guest_language=language)
    with transaction.atomic():
        originals.source.SOURCES[scenario][4].capture_event(stay.pk)
    stay.refresh_from_db()
    links, _ = financial.modules(scenario)
    type(row).objects.filter(pk=row.pk).update(fingerprint=links.fingerprint(stay))
    with CaptureQueriesContext(connection) as queries:
        value = read(prepared)
    assert value["available"], value
    assert value["financial_identity"]["delivery_id"] == str(row.pk)
    assert value["financial_identity"]["link_expires_at"] is None
    assert value["financial_identity"]["url_mode"] == "state_gated"
    assert value["financial_conditions_verified"] is True
    assert value["core_delivery_verified"] is True
    assert value["source_event_key"] == f"website:{scenario}:payment_link:{row.pk}"
    assert value["domain_authorized"] is value["dispatch_allowed"] is False
    assert value["reservation_revision"] == str(stay.communication_revision)
    assert value["variables"]["amount"] == "500.00"
    assert value["variables"]["currency"] == "SAR"
    assert value["variables"]["booking_reference"].startswith("AM-" if scenario == "07" else "DU-")
    assert calls == [
        {"guests": 2, "allow_started": True, "allow_elapsed": scenario == "07", "linked_stay": stay}
    ]
    assert not any(
        q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "ALTER", "CREATE"}
        for q in queries
    )
    row.refresh_from_db()
    assert row.state == "queued" and not row.provider_id


@pytest.mark.parametrize(
    "state", ["sending", "accepted", "uncertain", "failed", "delivered", "read"]
)
def test_no_adoption_or_retry_of_any_attempt(prepared, state):
    type(prepared[3]).objects.filter(pk=prepared[3].pk).update(state=state)
    assert read(prepared)["available"] is False
    assert not prepared[4]


@pytest.mark.parametrize(
    "field,value",
    [
        ("guest_phone", "+16502539999"),
        ("amount_sar", Decimal("501.00")),
        ("communication_revision", 20),
        ("status", "cancelled"),
        ("paid_at", timezone.now()),
        ("environment", "test"),
    ],
)
def test_current_booking_must_match_original_fingerprint_and_snapshot(prepared, field, value):
    type(prepared[1]).objects.filter(pk=prepared[1].pk).update(**{field: value})
    assert read(prepared)["available"] is False
    assert not prepared[4]


def test_all_three_deliveries_required_even_when_group_was_handed_off(prepared, monkeypatch):
    monkeypatch.setattr(financial, "core_delivered", lambda *_: False)
    result = read(prepared)
    assert result["domain_authorized"] is False
    assert "core_delivery_unverified" in result["blockers"]
    assert "variables" not in result
    assert not prepared[4]


def test_inventory_failure_and_conflict_never_return_authority(prepared, monkeypatch):
    _, domain = financial.modules(prepared[0])
    monkeypatch.setattr(
        domain, "verify_window", lambda *a, **k: (_ for _ in ()).throw(ValueError())
    )
    assert read(prepared)["available"] is False
    monkeypatch.setattr(domain, "verify_window", lambda *a, **k: None)
    monkeypatch.setattr(domain, "local_inflight_conflict", lambda *a: True)
    assert read(prepared)["reason"] == "current_inventory_conflict"


def test_no_prior_ambiguous_send_can_be_replaced_with_new_uuid(prepared):
    prepared[1].payment_link_deliveries.create(
        fingerprint=prepared[3].fingerprint, state="uncertain"
    )
    assert read(prepared)["reason"] == "prior_delivery_requires_reconciliation"
    assert not prepared[4]


def test_ended_stay_is_not_eligible(prepared):
    type(prepared[1]).objects.filter(pk=prepared[1].pk).update(
        starts_at=timezone.now() - timedelta(hours=4),
        ends_at=timezone.now() - timedelta(seconds=1),
    )
    assert read(prepared)["available"] is False


def test_confirmation_requires_current_actual_money_not_a_message(prepared):
    scenario, stay, event, _, _ = prepared
    assert financial.payment_confirmation_source(uuid4(), scenario)["available"] is False
    _, stay, event = originals.paid_record((scenario, stay, event))
    result = financial.payment_confirmation_source(event.pk, scenario)
    assert result["available"], result
    assert result["financial_identity"]["proof_key"] == event.proof_key
    assert result["financial_identity"]["payment_status"] == "paid"
    assert result["purpose"] == "payment_confirmation"
    assert result["dispatch_allowed"] is False
    type(stay).objects.filter(pk=stay.pk).update(amount_sar=Decimal("501.00"))
    assert financial.payment_confirmation_source(event.pk, scenario)["available"] is False


def test_closed_adapter_does_not_change_live_source_endpoint(client, record):
    assert originals.request_source(client, record).json()["dispatch_allowed"] is False


def link_request(client, prepared, token=None, extra=None):
    scenario, stay, event, row, _ = prepared
    comms = originals.source.SOURCES[scenario][4]
    prefix = "morning-pay-" if scenario == "07" else "day-use-pay-"
    return client.post(
        reverse(
            "integrations:"
            + ("morning07" if scenario == "07" else "day_use08")
            + "_payment_link_source_authorization"
        ),
        {"job_sha256": originals.source.sha(prefix + str(row.pk)), **(extra or {})},
        content_type="application/json",
        headers={
            f"X-LSA-{'Morning07' if scenario == '07' else 'DayUse08'}-Token": token
            or comms.event_token(event)
        },
    )


def test_link_read_is_scoped_and_strict_without_new_credential(client, prepared):
    good = link_request(client, prepared)
    assert good.status_code == 200, good.content
    assert good.json()["dispatch_allowed"] is False
    assert good["Cache-Control"].startswith("private, no-store")
    assert good["Referrer-Policy"] == "no-referrer"
    assert link_request(client, prepared, token="forged").status_code == 404
    scenario, stay, _, _, _ = prepared
    send_token = originals.source.SOURCES[scenario][4].delivery_token(stay)
    assert link_request(client, prepared, token=send_token).status_code == 404
    assert link_request(client, prepared, extra={"accepted": True}).status_code == 400


def test_link_read_cannot_cross_event_revision(client, prepared):
    scenario, stay, event, _, _ = prepared
    type(stay).objects.filter(pk=stay.pk).update(guest_name="New synthetic name")
    originals.source.SOURCES[scenario][4].capture_event(stay.pk)
    assert link_request(client, prepared).status_code == 409


def test_link_read_obeys_existing_gate_and_channel_ownership(
    client, prepared, settings, monkeypatch
):
    monkeypatch.setattr("apps.reservations.telegram_stay_transport.selected", lambda _: True)
    assert link_request(client, prepared).json()["code"] == "telegram_owned_stay"
    settings.MESSAGE_SOURCE_AUTH_ENABLED = False
    assert link_request(client, prepared).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_postgres_holds_source_lock_through_live_availability(prepared, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections

    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL row locks")
    scenario, stay, *_ = prepared
    _, domain = financial.modules(scenario)
    started, finished = Event(), Event()
    futures = []

    def change_source():
        close_old_connections()
        try:
            started.set()
            return type(stay).objects.filter(pk=stay.pk).update(amount_sar=Decimal("501.00"))
        finally:
            finished.set()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def availability(*args, **kwargs):
            futures.append(pool.submit(change_source))
            assert started.wait(2)
            assert not finished.wait(0.1)

        monkeypatch.setattr(domain, "verify_window", availability)
        value = read(prepared)
        assert value["available"], value
        assert futures[0].result(timeout=3) == 1
    assert read(prepared)["available"] is False
