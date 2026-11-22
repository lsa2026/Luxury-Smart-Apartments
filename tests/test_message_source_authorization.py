"""Synthetic source records only. No claim, send, booking or payment provider."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.reservations import message_source_authorization as source
from apps.reservations.models import (
    DayUseOfflineReceipt,
    MorningOfflineReceipt,
)
from tests.test_availability_service import make_property

pytestmark = pytest.mark.django_db


@pytest.fixture(params=["07", "08"])
def record(request, settings, monkeypatch):
    scenario = request.param
    settings.MORNING07_N8N_ENABLED = False
    settings.MORNING07_PAYMENT_HANDOFF_ENABLED = False
    settings.DAY_USE08_ENABLED = False
    settings.MESSAGE_SOURCE_AUTH_ENABLED = True
    settings.BOOKING_WHATSAPP_CHOICE_ENABLED = False
    settings.SITE_BASE_URL = "https://example.invalid"
    monkeypatch.setattr("httpx.Client.request", lambda *a, **k: pytest.fail("network forbidden"))
    model, _, _, _, comms, payments = source.SOURCES[scenario]
    monkeypatch.setattr(comms, "queue_stay_event", lambda *a: None)
    monkeypatch.setattr(payments, "queue_event", lambda *a: None)
    prop = make_property()
    prop.hostaway_listing_map_id = 97000 + int(scenario)
    prop.save()
    actor = get_user_model().objects.create_user(username="synthetic-source-operator")
    start = (timezone.now() + timedelta(days=2)).replace(hour=6, minute=0, second=0, microsecond=0)
    values = dict(
        request_id=uuid4(),
        property=prop,
        starts_at=start,
        ends_at=start + timedelta(hours=3),
        expires_at=start,
        guest_name="Synthetic Guest",
        guest_phone="+16502530000",
        guest_email="synthetic@example.invalid",
        guest_language="en",
        guests=2,
        amount_sar=Decimal("500.00"),
        environment="production",
        status="open",
        created_by=actor,
        availability_checked_at=timezone.now(),
        availability_evidence={
            "creation_mode": "hostaway_first_v2",
            "hostaway_morning" if scenario == "07" else "hostaway_day_use": {
                "state": "confirmed",
                "reservation_id": 99007,
            },
        },
    )
    if scenario == "08":
        values.update(
            inventory_arrival_date=start.date() - timedelta(days=1),
            inventory_departure_date=start.date() + timedelta(days=1),
        )
    stay = model.objects.create(**values)
    settings.MORNING07_N8N_ENABLED = True
    settings.MORNING07_PAYMENT_HANDOFF_ENABLED = True
    settings.DAY_USE08_ENABLED = True
    with transaction.atomic():
        comms.capture_event(stay.pk)
    stay.refresh_from_db()
    return scenario, stay, stay.latest_communication_event()


def request_source(
    client, record, purpose="house_rules", *, financial=False, token=None, body=None
):
    scenario, stay, event = record
    payments = source.SOURCES[scenario][5]
    comms = source.SOURCES[scenario][4]
    name = ("morning07" if scenario == "07" else "day_use08") + (
        "_payment_source_authorization" if financial else "_source_authorization"
    )
    key = (
        payments.reference(event.pk, purpose)[0]
        if financial
        else source.core_keys(stay.pk, event.payload, scenario)[purpose]
    )
    header = (
        payments.HEADER
        if financial
        else f"X-LSA-{'Morning07' if scenario == '07' else 'DayUse08'}-Token"
    )
    token = (
        token
        if token is not None
        else (payments.capability(event) if financial else comms.event_token(event))
    )
    return client.post(
        reverse("integrations:" + name),
        body if body is not None else {"job_sha256": source.sha(key)},
        content_type="application/json",
        headers={header: token},
    )


def test_source_is_read_only_and_unknown_facts_never_grant_dispatch(client, record):
    scenario, stay, event = record
    with CaptureQueriesContext(connection) as queries:
        result = request_source(client, record)
    assert result.status_code == 200
    assert not any(
        q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries
    )
    facts = result.json()
    assert facts["source_family"] == "website" and facts["scenario"] == scenario
    assert facts["reservation_revision"] == str(stay.communication_revision)
    assert facts["environment"] == "production"
    assert facts["current_source_verified"] and facts["domain_authorized"]
    assert facts["dispatch_allowed"] is False and facts["legacy_state"] == "not_attempted"
    assert facts["consent"]["service"] is facts["consent"]["marketing"] is None
    assert facts["human_takeover"] is facts["message"] is facts["render_policy_ref"] is None
    assert facts["source_occurred_at"] == event.created_at.isoformat()
    assert facts["recipient_e164"] == stay.guest_phone
    assert "guest_email" not in result.content.decode() and "billing" not in result.content.decode()
    assert result["Cache-Control"].startswith("private, no-store")
    assert result["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"job_sha256": "a" * 64, "consent": True},
        {"job_sha256": "A" * 64},
        {"job_sha256": []},
        [],
        "not-json",
    ],
)
def test_strict_body(client, record, body):
    assert request_source(client, record, body=body).status_code == 400


def test_capabilities_cannot_cross_scope_or_use_send_token(client, record, settings):
    scenario, stay, _ = record
    assert request_source(client, record, token="forged").status_code == 404
    assert (
        request_source(
            client, record, token=source.SOURCES[scenario][4].delivery_token(stay)
        ).status_code
        == 404
    )
    assert (
        request_source(
            client, record, token=signing.TimestampSigner(salt="wrong").sign(str(stay.pk))
        ).status_code
        == 404
    )
    settings.MESSAGE_SOURCE_AUTH_ENABLED = False
    assert request_source(client, record).status_code == 404


def test_expired_read_capability(client, record, monkeypatch):
    scenario, _, event = record
    with monkeypatch.context() as m:
        m.setattr("django.core.signing.time.time", lambda: 1000)
        token = source.SOURCES[scenario][4].event_token(event)
    assert request_source(client, record, token=token).status_code == 404


@pytest.mark.parametrize("change", ["phone", "date", "amount", "revision", "environment", "ended"])
def test_unversioned_and_stale_changes_hold(client, record, change):
    scenario, stay, _ = record
    values = {
        "phone": {"guest_phone": "+442079460018"},
        "date": {"ends_at": stay.ends_at + timedelta(hours=1)},
        "amount": {"amount_sar": Decimal("600")},
        "revision": {"communication_revision": 2},
        "environment": {"environment": "sandbox"},
        "ended": {
            "starts_at": timezone.now() - timedelta(days=2),
            "ends_at": timezone.now() - timedelta(days=1),
        },
    }[change]
    source.SOURCES[scenario][0].objects.filter(pk=stay.pk).update(**values)
    assert request_source(client, record).status_code == 409


def test_old_phone_delivery_and_history_gap_never_appear_new(client, record):
    scenario, stay, event = record
    _, _, _, attempts, comms, _ = source.SOURCES[scenario]
    old_key = source.core_keys(stay.pk, event.payload, scenario)["house_rules"]
    attempts.objects.create(
        reference_id=source.legacy_reference(old_key, scenario),
        stay=stay,
        revision=1,
        payload_hash="a" * 64,
        state="uncertain",
    )
    source.SOURCES[scenario][0].objects.filter(pk=stay.pk).update(guest_phone="+442079460018")
    with transaction.atomic():
        comms.capture_event(stay.pk)
    stay.refresh_from_db()
    updated = scenario, stay, stay.latest_communication_event()
    facts = request_source(client, updated).json()
    assert facts["legacy_state"] == "uncertain"
    assert facts["semantic_key"] == source.sha(f"website:{scenario}:{stay.pk}:house_rules")
    event.delete()
    assert request_source(client, updated).json()["legacy_state"] == "unknown"


def test_checkout_time_is_one_hour_before_actual_end(client, record):
    result = request_source(client, record, "reminder_checkout").json()
    assert result["not_before"] == (record[1].ends_at - timedelta(hours=1)).isoformat()


@pytest.mark.parametrize("legacy", ["accepted", "uncertain"])
def test_canonical_export_preserves_legacy_status_key_and_receipt(client, record, legacy):
    scenario, stay, event = record
    key = source.core_keys(stay.pk, event.payload, scenario)["status_new"]
    source.SOURCES[scenario][3].objects.create(
        reference_id=source.legacy_reference(key, scenario),
        stay=stay,
        revision=1,
        payload_hash="a" * 64,
        state=legacy,
    )
    facts = request_source(client, record, "status_new").json()
    assert facts["purpose"] == "booking_confirmation"
    assert facts["source_message_type"] == "status_new"
    assert facts["legacy_state"] == legacy
    assert facts["semantic_key"] == source.sha(f"website:{scenario}:{stay.pk}:status_new")
    assert facts["semantic_key_version"] == "website-message-slot-v1"


def paid_record(record):
    scenario, stay, _ = record
    actor = get_user_model().objects.create_user(username="synthetic-operator")
    paid_at = timezone.now()
    (MorningOfflineReceipt if scenario == "07" else DayUseOfflineReceipt).objects.create(
        stay=stay, amount=stay.amount_sar, method="cash", received_at=paid_at, recorded_by=actor
    )
    source.SOURCES[scenario][0].objects.filter(pk=stay.pk).update(status="paid", paid_at=paid_at)
    with transaction.atomic():
        source.SOURCES[scenario][5].capture_payment(stay.pk)
    stay.refresh_from_db()
    return scenario, stay, stay.payment_events.get()


def test_financial_proof_is_rechecked_and_delivery_dependencies_hold(client, record):
    paid = paid_record(record)
    result = request_source(client, paid, "payment_success", financial=True)
    assert result.status_code == 200
    facts = result.json()
    assert facts["payment_verified"] is True and facts["payment_status"] == "paid"
    assert facts["domain_authorized"] is False and facts["dispatch_allowed"] is False
    assert "Payment received successfully" in facts["message"]
    assert facts["legacy_state"] == "not_attempted"
    payments = source.SOURCES[paid[0]][5]
    assert (
        request_source(
            client,
            paid,
            "payment_success",
            financial=True,
            token=payments.capability(paid[2], delivery=True),
        ).status_code
        == 404
    )
    source.SOURCES[paid[0]][0].objects.filter(pk=paid[1].pk).update(amount_sar=Decimal("999"))
    assert request_source(client, paid, "payment_success", financial=True).status_code == 409


def test_loyalty_never_inherits_service_consent(client, record):
    facts = request_source(client, paid_record(record), "loyalty_invitation", financial=True).json()
    assert facts["consent"]["marketing"] is None and facts["domain_authorized"] is False
    assert "loyalty_registry_and_marketing_consent_required" in facts["blockers"]
