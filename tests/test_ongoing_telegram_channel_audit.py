"""Read-only ongoing phase counts and the runtime's fixed timezone conversion."""

import importlib
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests import test_message_source_authorization as original

audit = importlib.import_module(
    "apps.reservations.migrations.0043_ongoing_telegram_channel_aggregate_audit"
)
record = original.record
pytestmark = pytest.mark.django_db


def historical():
    return (
        MigrationExecutor(connection)
        .loader.project_state([("reservations", "0043_ongoing_telegram_channel_aggregate_audit")])
        .apps
    )


def run(apps, cutoff, capsys):
    with override_settings(MESSAGE_SOURCE_CUTOVER_AT=cutoff):
        with CaptureQueriesContext(connection) as queries:
            audit.audit_ongoing_channels(apps, SimpleNamespace(connection=connection))
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith(audit.PREFIX)
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries)
    sql = "\n".join(q["sql"].lower() for q in queries)
    for private in (
        "guest_phone",
        "guest_name",
        "payload",
        "identity",
        "booking_basis",
        "provider_message_id",
    ):
        assert private not in sql
    return json.loads(lines[0][len(audit.PREFIX) :])


def setup(record):
    scenario, stay, _ = record
    now = timezone.now()
    cutoff = now - timedelta(hours=2)
    type(stay).objects.filter(pk=stay.pk).update(
        starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1)
    )
    apps = historical()
    channel = apps.get_model("reservations", "TelegramStayChannel").objects.create(
        scenario=scenario,
        stay_id=stay.pk,
        hostaway_id="1234567",
        cutover_at=cutoff,
        selected_at=cutoff - timedelta(seconds=1),
    )
    return scenario, stay, apps, channel, cutoff


def test_empty_counts_are_explicit_and_runtime_probe_is_fixed(capsys):
    value = run(historical(), timezone.now().isoformat(), capsys)
    assert value["complete"] is True and len(value["counts"]) == 4
    assert all(row["ongoing_legacy_channels"] == 0 for row in value["counts"])
    assert value["domain_rows_modified"] is False
    assert value["native_provider_nonattempted_verified"] is False
    assert value["eligibility_evaluated"] is False
    probe = value["runtime_timezone_probe"]
    assert probe["zone"] == "Africa/Casablanca" and probe["available"] is True
    assert probe["input_utc"] == "2026-11-24T12:00:00+00:00"
    assert isinstance(probe["offset_minutes"], int)


@pytest.mark.parametrize(
    "marker",
    [
        "none",
        "notice",
        "checkout_attempt",
        "review_attempt",
        "review_issued",
        "review_unknown",
        "review_held",
        "unrelated_core",
    ],
)
def test_phase_buckets_preserve_uncertainty_and_never_read_private_rows(record, capsys, marker):
    scenario, stay, apps, _, cutoff = setup(record)
    if marker == "notice":
        apps.get_model("reservations", "TelegramStayNotice").objects.create(
            scenario=scenario, stay_id=stay.pk, event_id=uuid.uuid4(), revision=1, attempts=1
        )
    elif marker != "none":
        review = marker.startswith("review_")
        apps.get_model("reservations", "TelegramStayMessage").objects.create(
            scenario=scenario,
            stay_id=stay.pk,
            event_id=uuid.uuid4(),
            revision=1,
            purpose="review_request"
            if review
            else "house_rules"
            if marker == "unrelated_core"
            else "reminder_checkout",
            sequence=1,
            job_key="synthetic-job",
            reference_id="synthetic-ref",
            identity={"private": "SYNTHETIC_PRIVATE_MARKER"},
            booking_basis={"private": "SYNTHETIC_PRIVATE_MARKER"},
            state="unknown"
            if marker == "review_unknown"
            else "held"
            if marker == "review_held"
            else "planned",
            content_sha256="f" * 64 if marker == "review_issued" else "",
            attempt_id=uuid.uuid4()
            if marker in {"checkout_attempt", "review_attempt", "unrelated_core"}
            else None,
            not_before=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
    value = run(apps, cutoff.isoformat(), capsys)
    for row in (r for r in value["counts"] if r["scenario"] == scenario):
        expected = "pristine_local"
        if marker == "notice" or (
            row["phase"] == "reviews"
            and marker in {"review_issued", "review_unknown", "review_held"}
        ):
            expected = "uncertain"
        if marker == row["phase"].removesuffix("s") + "_attempt":
            expected = "recorded_attempt"
        assert row == {
            "scenario": scenario,
            "phase": row["phase"],
            "ongoing_legacy_channels": 1,
            **{k: int(k == expected) for k in ("pristine_local", "recorded_attempt", "uncertain")},
        }
    assert str(stay.pk) not in json.dumps(value)
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(value)


@pytest.mark.parametrize("excluded", ["future_start", "ended", "new_selection"])
def test_scope_is_not_widened(record, capsys, excluded):
    scenario, stay, apps, channel, cutoff = setup(record)
    if excluded == "new_selection":
        type(channel).objects.filter(pk=channel.pk).update(
            selected_at=cutoff + timedelta(seconds=1)
        )
    elif excluded == "future_start":
        type(stay).objects.filter(pk=stay.pk).update(
            starts_at=timezone.now() + timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=2),
        )
    else:
        type(stay).objects.filter(pk=stay.pk).update(ends_at=timezone.now() - timedelta(seconds=1))
    value = run(apps, cutoff.isoformat(), capsys)
    assert all(
        r["ongoing_legacy_channels"] == 0 for r in value["counts"] if r["scenario"] == scenario
    )


@pytest.mark.parametrize("cutoff", ["", "SYNTHETIC_SECRET_MARKER", "2026-11-24T01:15:00"])
def test_unknown_source_is_not_zero(cutoff, capsys):
    value = run(historical(), cutoff, capsys)
    assert value["complete"] is False and value["counts"] is None
    assert "SYNTHETIC_SECRET_MARKER" not in json.dumps(value)


def test_reverse_is_noop():
    (op,) = audit.Migration.operations
    assert op.reverse_code is audit.migrations.RunPython.noop
