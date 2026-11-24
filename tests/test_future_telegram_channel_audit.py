"""Only aggregate metadata crosses the existing predeploy/log boundary."""

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
    "apps.reservations.migrations.0042_future_telegram_channel_aggregate_audit"
)
record = original.record
pytestmark = pytest.mark.django_db


def historical():
    return (
        MigrationExecutor(connection)
        .loader.project_state([("reservations", "0042_future_telegram_channel_aggregate_audit")])
        .apps
    )


def run(apps, cutoff, capsys):
    with override_settings(MESSAGE_SOURCE_CUTOVER_AT=cutoff):
        with CaptureQueriesContext(connection) as queries:
            audit.audit_future_channels(apps, SimpleNamespace(connection=connection))
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith(audit.PREFIX)
    sql = "\n".join(q["sql"].lower() for q in queries)
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries)
    for private in (
        "guest_phone",
        "guest_name",
        "payload",
        "identity",
        "booking_basis",
        "provider_message_id",
        "content_sha256",
    ):
        assert private not in sql
    return json.loads(lines[0][len(audit.PREFIX) :])


def setup(record):
    scenario, stay, _ = record
    now = timezone.now()
    cutoff = now - timedelta(hours=1)
    type(stay).objects.filter(pk=stay.pk).update(
        starts_at=now + timedelta(hours=1), ends_at=now + timedelta(hours=3)
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


def test_no_channel_has_explicit_zero_counts_without_reading_guests(capsys):
    value = run(historical(), timezone.now().isoformat(), capsys)
    assert value["complete"] is True
    assert all(row["future_legacy_channels"] == 0 for row in value["counts"])
    assert value["domain_rows_modified"] is False
    assert value["native_provider_nonattempted_verified"] is False


@pytest.mark.parametrize(
    "marker", ["none", "notice", "private", "workgroup", "unknown", "multiple"]
)
def test_channel_count_is_distinct_and_attempt_buckets_are_conservative(record, capsys, marker):
    scenario, stay, apps, _, cutoff = setup(record)
    if marker in {"notice", "multiple"}:
        apps.get_model("reservations", "TelegramStayNotice").objects.create(
            scenario=scenario, stay_id=stay.pk, event_id=uuid.uuid4(), revision=1, attempts=1
        )
    for name in (
        ["TelegramStayMessage", "TelegramWorkgroupHandoff"]
        if marker == "multiple"
        else ["TelegramStayMessage"]
        if marker in {"private", "unknown"}
        else ["TelegramWorkgroupHandoff"]
        if marker == "workgroup"
        else []
    ):
        apps.get_model("reservations", name).objects.create(
            scenario=scenario,
            stay_id=stay.pk,
            event_id=uuid.uuid4(),
            revision=1,
            purpose="status_new",
            sequence=1,
            job_key="test-" + name,
            reference_id="ref-" + name,
            identity={"private": "SYNTHETIC_PRIVATE_MARKER"},
            booking_basis={"private": "SYNTHETIC_PRIVATE_MARKER"},
            state="unknown" if marker == "unknown" else "planned",
            attempt_id=None if marker == "unknown" else uuid.uuid4(),
            not_before=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
    value = run(apps, cutoff.isoformat(), capsys)
    row = next(row for row in value["counts"] if row["scenario"] == scenario)
    assert row == {
        "scenario": scenario,
        "future_legacy_channels": 1,
        "zero_recorded_local_attempt_markers": int(marker == "none"),
        "nonzero_recorded_local_attempt_markers": int(marker != "none"),
    }
    assert str(stay.pk) not in json.dumps(value)
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(value)


@pytest.mark.parametrize("excluded", ["selected_after_cutover", "already_started"])
def test_historical_selection_and_future_scope_are_not_widened(record, capsys, excluded):
    scenario, stay, apps, channel, cutoff = setup(record)
    if excluded == "selected_after_cutover":
        type(channel).objects.filter(pk=channel.pk).update(
            selected_at=cutoff + timedelta(seconds=1)
        )
    else:
        type(stay).objects.filter(pk=stay.pk).update(starts_at=timezone.now() - timedelta(hours=1))
    value = run(apps, cutoff.isoformat(), capsys)
    assert (
        next(row for row in value["counts"] if row["scenario"] == scenario)[
            "future_legacy_channels"
        ]
        == 0
    )


@pytest.mark.parametrize("cutoff", ["", "SYNTHETIC_SECRET_MARKER", "2026-11-24T01:15:00"])
def test_unknown_cutover_is_not_reported_as_zero(cutoff, capsys):
    value = run(historical(), cutoff, capsys)
    assert value["complete"] is False and value["counts"] is None
    assert value["reason"] == "source_cutover_unavailable"
    assert "SYNTHETIC_SECRET_MARKER" not in json.dumps(value)


def test_migration_is_data_read_only_and_reverse_noop():
    (op,) = audit.Migration.operations
    assert op.reverse_code is audit.migrations.RunPython.noop


def test_missing_source_does_not_produce_a_complete_or_zero_report(capsys):
    apps = historical()

    class MissingSource:
        def get_model(self, app, name):
            if name == "DayUseStay":
                raise LookupError("source unavailable")
            return apps.get_model(app, name)

    with override_settings(MESSAGE_SOURCE_CUTOVER_AT=timezone.now().isoformat()):
        with pytest.raises(LookupError):
            audit.audit_future_channels(MissingSource(), SimpleNamespace(connection=connection))
    assert capsys.readouterr().out == ""
