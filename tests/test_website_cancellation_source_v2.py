"""Project original cancellation confirmation, without external booking or money actions."""

import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.reservations import message_source_authorization as source
from apps.reservations import website_cancellation_source_v2 as cancellation
from apps.reservations.models import DayUseHostawayOwnership
from tests import test_message_source_authorization as original

record = original.record
pytestmark = pytest.mark.django_db


@pytest.fixture
def prepared(record, settings, monkeypatch):
    scenario, stay, event = record
    from apps.reservations import day_use_hostaway, day_use_ownership, morning_hostaway

    owner = morning_hostaway if scenario == "07" else day_use_hostaway
    stay.property.name_en = "Synthetic Cancellation Apartment"
    stay.property.save(update_fields=["name_en"])
    expected = owner._request(stay).payload
    operation = owner.booking_state(stay) | {
        "arrival_date": expected["arrivalDate"],
        "departure_date": expected["departureDate"],
        "amount_sar": str(stay.amount_sar),
        "actor_id": stay.created_by_id,
        "requested_at": (timezone.now() - timedelta(minutes=2)).isoformat(),
        "recorded_at": (timezone.now() - timedelta(minutes=1)).isoformat(),
    }
    owner._write_state(stay, operation)
    if scenario == "08":
        DayUseHostawayOwnership.objects.create(
            stay=stay,
            state="confirmed",
            revision=2,
            acknowledged_revision=2,
            provider_reservation_id=operation["reservation_id"],
        )
        monkeypatch.setattr(day_use_ownership, "queue", lambda *a: None)
    remote = Mock()
    values = dict(
        listing_map_id=stay.property.hostaway_listing_map_id,
        source=owner.PROVIDER,
        check_in=date.fromisoformat(operation["arrival_date"]),
        check_out=date.fromisoformat(operation["departure_date"]),
        status="confirmed",
    )
    remote.get_reservation.return_value = SimpleNamespace(**values)
    getattr(
        remote, "cancel_morning_reservation" if scenario == "07" else "cancel_day_use_reservation"
    ).return_value = SimpleNamespace(**(values | {"status": "cancelled"}))
    settings.MESSAGE_SOURCE_AUTH_ENABLED = True
    settings.MESSAGE_SOURCE_V2_ENABLED = True
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.MESSAGE_SOURCE_CUTOVER_AT = (timezone.now() - timedelta(minutes=1)).isoformat()
    settings.MESSAGE_CANCELLATION_SOURCE_V2_ENABLED = True
    settings.MESSAGE_CANCELLATION_SOURCE_CUTOVER_AT = settings.MESSAGE_SOURCE_CUTOVER_AT
    return scenario, stay, owner, remote


@pytest.fixture
def cancelled(prepared):
    scenario, stay, owner, remote = prepared
    result = owner.cancel_linked_stay(stay.pk, client=remote)
    event = result.latest_communication_event()
    assert event.payload["event_type"] == "cancelled"
    return scenario, result, event, owner, remote


def fields(cancelled):
    scenario, stay, event, _, _ = cancelled
    return cancellation.verified_fields(stay, event, scenario)


def facts(cancelled):
    scenario, stay, event, _, _ = cancelled
    key = source.core_keys(stay.pk, event.payload, scenario)["status_cancelled"]
    return source.core_facts(stay, event, scenario, source.sha(key))


def test_original_confirmed_cancel_is_the_authority_and_does_not_claim_a_refund(cancelled):
    scenario, stay, event, owner, remote = cancelled
    with CaptureQueriesContext(connection) as queries:
        evidence = fields(cancelled)
    assert evidence["final_cancellation_verified"] is True
    assert evidence["final_cancellation_source"]["provider_confirmed_at"] is None
    assert evidence["final_cancellation_source"]["source_event_at"] == event.created_at.isoformat()
    assert evidence["final_cancellation_evidence_ref"].startswith("site01-final-cancel-v1:")
    assert not any(
        q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries
    )
    encoded = json.dumps(evidence)
    assert stay.guest_phone not in encoded and stay.guest_email not in encoded
    assert facts(cancelled)["domain_authorized"] is True
    assert facts(cancelled)["final_cancellation_verified"] is True
    assert remote.get_reservation.call_count == 1
    assert (
        getattr(
            remote,
            "cancel_morning_reservation" if scenario == "07" else "cancel_day_use_reservation",
        ).call_count
        == 1
    )
    assert owner.booking_state(stay)["state"] == "cancelled"
    assert not stay.attempts.exists() and not stay.payment_link_deliveries.exists()


@pytest.mark.parametrize(
    "change",
    [
        "status_only",
        "unknown",
        "provider",
        "dates",
        "amount",
        "naive_time",
        "future_time",
        "revision",
        "environment",
    ],
)
def test_missing_or_crossed_original_cancellation_proof_never_becomes_true(cancelled, change):
    scenario, stay, event, owner, _ = cancelled
    operation = owner.booking_state(stay)
    if change == "status_only":
        operation["state"] = "confirmed"
    elif change == "unknown":
        operation["state"] = "cancel_unknown"
    elif change == "provider":
        operation["reservation_id"] += 1
    elif change == "dates":
        operation["arrival_date"] = "2020-01-01"
    elif change == "amount":
        operation["amount_sar"] = "1.00"
    elif change == "naive_time":
        operation["recorded_at"] = "2026-01-01T12:00:00"
    elif change == "future_time":
        operation["recorded_at"] = (timezone.now() + timedelta(days=1)).isoformat()
    elif change == "revision":
        type(stay).objects.filter(pk=stay.pk).update(
            communication_revision=stay.communication_revision + 1
        )
    else:
        type(stay).objects.filter(pk=stay.pk).update(environment="sandbox")
    owner._write_state(stay, operation)
    assert fields(cancelled)["final_cancellation_verified"] is None


def test_later_enablement_cannot_adopt_an_old_cancellation_event(cancelled, settings, monkeypatch):
    event = cancelled[2]
    settings.MESSAGE_CANCELLATION_SOURCE_CUTOVER_AT = (
        event.created_at + timedelta(seconds=1)
    ).isoformat()
    monkeypatch.setattr(
        cancellation.timezone, "now", lambda: event.created_at + timedelta(seconds=3)
    )
    assert fields(cancelled)["final_cancellation_verified"] is True
    assert facts(cancelled)["domain_authorized"] is False
    event.created_at += timedelta(hours=1)
    assert cancellation.admitted(event) is False


def test_final_proof_does_not_open_a_disabled_purpose(cancelled, settings):
    settings.MESSAGE_CANCELLATION_SOURCE_V2_ENABLED = False
    assert fields(cancelled)["final_cancellation_verified"] is True
    assert facts(cancelled)["domain_authorized"] is False


def test_uncertain_provider_cancel_keeps_local_booking_and_no_final_proof(prepared):
    scenario, stay, owner, remote = prepared
    getattr(
        remote, "cancel_morning_reservation" if scenario == "07" else "cancel_day_use_reservation"
    ).side_effect = TimeoutError("synthetic")
    error = owner.MorningStayError if scenario == "07" else owner.DayUseStayError
    with pytest.raises(error):
        owner.cancel_linked_stay(stay.pk, client=remote)
    stay.refresh_from_db()
    assert stay.status == "open" and owner.booking_state(stay)["state"] == "cancel_unknown"
    assert (
        cancellation.verified_fields(stay, stay.latest_communication_event(), scenario)[
            "final_cancellation_verified"
        ]
        is None
    )


def test_dayuse_owner_evidence_cannot_be_replaced_by_another_provider(cancelled):
    scenario, stay, _, _, _ = cancelled
    if scenario != "08":
        pytest.skip("Day-use-specific ownership")
    DayUseHostawayOwnership.objects.filter(stay=stay).update(provider_reservation_id=99999999)
    assert fields(cancelled)["final_cancellation_verified"] is None


def test_original_read_endpoint_exports_final_proof_without_writing_or_refunding(cancelled, client):
    scenario, stay, event, _, _ = cancelled
    with CaptureQueriesContext(connection) as queries:
        response = original.request_source(client, (scenario, stay, event), "status_cancelled")
    assert response.status_code == 200
    result = response.json()
    assert result["purpose"] == "booking_cancellation"
    assert result["final_cancellation_verified"] is True and result["domain_authorized"] is True
    assert result["dispatch_allowed"] is False
    assert response["Cache-Control"].startswith("private, no-store")
    assert response["Referrer-Policy"] == "no-referrer"
    assert not any(
        q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries
    )


def test_cancelled_template_is_independently_rebuilt_with_original_stay_values(cancelled):
    from copy import deepcopy

    from apps.reservations import website_financial_source_v2 as financial
    from apps.reservations import website_template_policy_v2 as policy
    from apps.reservations import website_template_selector_v2 as selector

    scenario, stay, event, _, _ = cancelled
    current = facts(cancelled)
    context = {key: current[key] for key in selector.IDENTITY}
    context.update(
        source_family=f"lsa{scenario}",
        recipient_ref=None,
        recipient_binding_sha256=source.sha(
            current["reservation_key"] + ":" + current["recipient_e164"]
        ),
        not_before=current["not_before"],
        expires_at=current["expires_at"],
        language=current["language"],
        values=financial.core_variables(event, scenario),
        source_context_ref="synthetic-private-runtime-cancel-context",
    )
    expected = policy.policy_from_source(event, current, context)
    assert expected and expected["message"]["mode"] == "template"
    assert expected["message"]["attachment"] is None
    assert expected["message"]["template_ref"] == "lsa_booking_cancelled_v2"
    assert stay.guest_name in expected["message"]["variables"]
    crossed = deepcopy(context)
    crossed["values"]["booking_reference"] = "OTHER-BOOKING"
    assert policy.policy_from_source(event, current, crossed) is None
    crossed = deepcopy(context)
    crossed["reservation_revision"] = str(int(current["reservation_revision"]) + 1)
    assert policy.policy_from_source(event, current, crossed) is None
