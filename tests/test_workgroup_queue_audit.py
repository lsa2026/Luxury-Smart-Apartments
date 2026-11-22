"""Historical-app aggregate audit, no provider calls or production records."""

import importlib
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests import test_message_source_authorization as source_tests

audit = importlib.import_module("apps.reservations.migrations.0040_workgroup_queue_aggregate_audit")
pytestmark = pytest.mark.django_db
record = source_tests.record


def historical():
    return (
        MigrationExecutor(connection)
        .loader.project_state([("reservations", "0040_workgroup_queue_aggregate_audit")])
        .apps
    )


def reports(capsys):
    lines = capsys.readouterr().out.splitlines()
    assert lines and all(line.startswith(audit.PREFIX) for line in lines)
    return [json.loads(line[len(audit.PREFIX) :]) for line in lines]


def run(apps):
    with CaptureQueriesContext(connection) as queries:
        audit.audit_queue_aggregates(apps, SimpleNamespace(connection=connection))
    assert queries.captured_queries
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries)
    sql = "\n".join(q["sql"].lower() for q in queries)
    for private_field in (
        "guest_phone",
        "guest_name",
        "payload",
        "booking_basis",
        "provider_id",
        "response",
        "identity",
    ):
        assert private_field not in sql


def test_empty_sources_report_explicit_totals_and_a_complete_manifest(capsys):
    run(historical())
    values = reports(capsys)
    assert len(values) == len(audit.SOURCES) + 1
    assert all(v["total"] == 0 and v["counts"] == [] for v in values[:-1])
    assert values[-1]["kind"] == "complete" and values[-1]["models"] == len(audit.SOURCES)
    assert values[-1]["domain_rows_modified"] is False


def test_private_receipts_and_group_handoffs_are_distinct_and_never_leak_payload(capsys):
    apps = historical()
    now = timezone.now()
    secret = "DO_NOT_LOG_GUEST_PHONE_TEXT_OR_CAPABILITY"
    base = {
        "scenario": "07",
        "stay_id": uuid.uuid4(),
        "event_id": uuid.uuid4(),
        "revision": 1,
        "purpose": "status_new",
        "sequence": 1,
        "identity": {"private": secret},
        "booking_basis": {"private": secret},
        "not_before": now,
        "expires_at": now + timedelta(hours=1),
    }
    private = apps.get_model("reservations", "TelegramStayMessage")
    handoff = apps.get_model("reservations", "TelegramWorkgroupHandoff")
    for n, state in enumerate(("accepted", "unknown", "uncertain")):
        private.objects.create(
            **base, job_key=f"audit-private-{n}", reference_id=f"audit-ref-{n}", state=state
        )
    handoff.objects.create(
        **base, job_key="audit-group", reference_id="audit-group-ref", state="handed_off"
    )
    private.objects.create(
        **{**base, "scenario": "99"},
        job_key="audit-other",
        reference_id="audit-other-ref",
        state="PRIVATE_LABEL",
    )
    run(apps)
    values = reports(capsys)
    assert secret not in json.dumps(values) and "PRIVATE_LABEL" not in json.dumps(values)
    rows = {v["model"]: v for v in values[:-1]}
    assert rows["reservations.TelegramStayMessage"]["counts"] == [
        {"scenario": "07", "state": "accepted", "count": 1},
        {"scenario": "07", "state": "uncertain", "count": 1},
        {"scenario": "07", "state": "unknown", "count": 1},
        {"scenario": "other", "state": "other", "count": 1},
    ]
    assert rows["reservations.TelegramWorkgroupHandoff"]["counts"] == [
        {"scenario": "07", "state": "handed_off", "count": 1}
    ]
    assert private.objects.count() == 4 and handoff.objects.count() == 1


def test_previous_guest_delivery_states_are_only_counts(record, capsys):
    scenario, stay, _ = record
    apps = historical()
    name = "Morning07DeliveryAttempt" if scenario == "07" else "DayUse08DeliveryAttempt"
    model = apps.get_model("reservations", name)
    model.objects.create(
        reference_id="audit-accepted",
        stay_id=stay.pk,
        revision=1,
        payload_hash="a" * 64,
        state="accepted",
        response={"secret": "HIDDEN"},
    )
    model.objects.create(
        reference_id="audit-unknown",
        stay_id=stay.pk,
        revision=1,
        payload_hash="b" * 64,
        state="unknown",
        response={"secret": "HIDDEN"},
    )
    run(apps)
    value = next(v for v in reports(capsys) if v.get("model") == "reservations." + name)
    assert value["evidence_kind"] == "legacy_private_guest_delivery"
    assert value["counts"] == [
        {"scenario": scenario, "state": "accepted", "count": 1},
        {"scenario": scenario, "state": "unknown", "count": 1},
    ]


def test_missing_source_is_not_zero_or_a_complete_audit(capsys):
    class Missing:
        def get_model(self, *args):
            raise LookupError("source unavailable")

    with pytest.raises(LookupError):
        audit.audit_queue_aggregates(Missing(), SimpleNamespace(connection=connection))
    assert capsys.readouterr().out == ""


def test_partial_read_never_emits_a_completion_marker(capsys):
    apps = historical()

    class Partial:
        def get_model(self, app, name):
            if name == "TelegramStayNotice":
                raise LookupError("second source unavailable")
            return apps.get_model(app, name)

    with pytest.raises(LookupError):
        audit.audit_queue_aggregates(Partial(), SimpleNamespace(connection=connection))
    values = reports(capsys)
    assert len(values) == 1 and values[0]["kind"] == "model_counts"


def test_reverse_is_noop_and_migration_contains_no_schema_operation(capsys):
    (operation,) = audit.Migration.operations
    assert operation.reverse_code is audit.migrations.RunPython.noop
    operation.reverse_code(None, None)
    assert capsys.readouterr().out == ""
