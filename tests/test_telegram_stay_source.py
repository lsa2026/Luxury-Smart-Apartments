"""Authenticated model snapshots only; no provider/bookings/payment operations."""

import json
import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.reservations.models import DayUseHostawayOwnership
from tests import test_loyalty_invitation_api as loyalty
from tests import test_message_source_authorization as source_tests

pytestmark = pytest.mark.django_db
auth = loyalty.auth
record = source_tests.record
PATH = "/integrations/telegram39/stays/source/"


@pytest.fixture
def source_record(record, auth, settings):
    settings.TELEGRAM_STAY_MESSAGES_ENABLED = True
    scenario, stay, event = record
    if scenario == "08":
        DayUseHostawayOwnership.objects.create(
            stay=stay,
            revision=2,
            acknowledged_revision=2,
            state="confirmed",
            provider_reservation_id=event.payload["hostaway_reservation_id"],
        )
    return record


def payload(record, **changes):
    scenario, stay, event = record
    return {
        "contract": "tg39_stay_source_request_v1",
        "scenario": scenario,
        "operation": "snapshot",
        "request_id": str(uuid.uuid4()),
        "event_id": str(event.pk),
        "stay_key": event.payload["reservation_key"],
    } | changes


def post(data, **headers):
    return Client(enforce_csrf_checks=True).post(
        PATH, data, content_type="application/json", secure=True, **(loyalty.HEADERS | headers)
    )


def test_source_snapshot_is_current_model_owned_read_only_and_phone_hashed(source_record):
    scenario, stay, event = source_record
    with CaptureQueriesContext(connection) as queries:
        reply = post(payload(source_record))
    assert reply.status_code == 200
    value = reply.json()
    assert value["decision"] == "snapshot" and "job" not in value
    assert value["source_owner"] == ("morning07" if scenario == "07" else "dayuse08")
    assert value["source_key"] == str(stay.pk) and value["ownership_verified"] is True
    assert value["event_id"] == str(event.pk) and value["source_revision"] == 1
    assert isinstance(value["hostaway_reservation_id"], str)
    assert len(value["booking"]["phone_hash"]) == 64
    assert stay.guest_phone.encode() not in reply.content
    assert "no-store" in reply["Cache-Control"] and reply["Referrer-Policy"] == "no-referrer"
    assert not any(
        q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for q in queries
    )


def test_next_does_not_invent_job_or_acceptance_before_sender_ready(source_record):
    result = post(payload(source_record, operation="next")).json()
    assert result["decision"] == "wait" and result["reason"] == "stay_dispatch_not_ready"
    assert "job" not in result and "core_accepted" not in result


@pytest.mark.parametrize(
    "case",
    ["extra_phone", "wrong_event", "wrong_key", "raw_id", "old_revision", "ended", "wrong_owner"],
)
def test_unverified_source_identity_and_window_are_held(source_record, case):
    _, stay, _ = source_record
    data = payload(source_record)
    if case == "extra_phone":
        data["phone"] = stay.guest_phone
    elif case == "wrong_event":
        data["event_id"] = str(uuid.uuid4())
    elif case == "wrong_key":
        data["stay_key"] = data["stay_key"].split(":")[0] + ":" + str(uuid.uuid4())
    elif case == "raw_id":
        data["stay_key"] = "99007"
    elif case == "old_revision":
        type(stay).objects.filter(pk=stay.pk).update(communication_revision=2)
    elif case == "ended":
        type(stay).objects.filter(pk=stay.pk).update(
            starts_at=timezone.now() - timedelta(hours=3),
            ends_at=timezone.now() - timedelta(hours=1),
        )
    else:
        data["scenario"] = "08" if data["scenario"] == "07" else "07"
    result = post(data)
    assert result.status_code == 409 and b"job" not in result.content


def test_source_needs_existing_auth_and_explicit_flag(source_record, settings):
    assert Client().post(PATH, {}, content_type="application/json", secure=True).status_code == 401
    settings.TELEGRAM_STAY_MESSAGES_ENABLED = False
    assert post(payload(source_record)).status_code == 404


def test_dayuse_snapshot_needs_actual_acknowledged_ownership(source_record):
    scenario, stay, _ = source_record
    if scenario == "07":
        return
    DayUseHostawayOwnership.objects.filter(stay=stay).update(acknowledged_revision=1)
    assert post(payload(source_record)).status_code == 409


def test_duplicate_json_cannot_override_source_owner(source_record):
    body = json.dumps(payload(source_record))
    body = body[:-1] + ',"scenario":"08"}'
    assert post(body).status_code == 409
