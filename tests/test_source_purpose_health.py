"""Operational readback must not become an environment dump or dispatch operation."""

import json
import logging
from types import SimpleNamespace

import pytest
from django.test import RequestFactory, override_settings

from apps.notifications.health import live
from scheduler_runtime.source_purpose_health import FIELDS, log_snapshot, snapshot
from tests.test_scheduler_isolation import run_isolated

EPOCH = "2026-11-24T02:45:00Z"
VALUES = {key: True if key.endswith("_ENABLED") else EPOCH for key in FIELDS}


def test_public_probe_reads_resolved_settings_without_database_or_dispatch(monkeypatch):
    # No django_db marker: any accidental query fails this test.
    monkeypatch.setenv(FIELDS[0], "false")
    with override_settings(**VALUES, SAMA_API_KEY="SYNTHETIC_SECRET_MARKER"):
        response = live(RequestFactory().get("/health/live/"))
    assert response.status_code == 200
    assert json.loads(response.content) == {"status": "ok", "website_source_purposes": VALUES}
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "no-referrer"
    assert b"SYNTHETIC_SECRET_MARKER" not in response.content


def test_environment_read_is_limited_to_six_known_keys():
    class LimitedEnvironment:
        def get(self, key, default):
            assert key in FIELDS
            return "true" if key.endswith("_ENABLED") else EPOCH

        def items(self):
            raise AssertionError("An environment dump is forbidden")

    assert snapshot(environment=LimitedEnvironment()) == VALUES


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", " TRUE "])
def test_beat_reports_explicit_enabled_booleans(raw):
    result = snapshot(environment={FIELDS[0]: raw})
    assert result[FIELDS[0]] is True
    assert result[FIELDS[2]] is False


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", " FALSE "])
def test_beat_reports_explicit_disabled_booleans(raw):
    assert snapshot(environment={FIELDS[0]: raw})[FIELDS[0]] is False


@pytest.mark.parametrize(
    "raw",
    [
        "SYNTHETIC_SECRET_MARKER",
        "2026-11-24T02:45:00",
        "2026-99-24T02:45:00Z",
        "2026-11-24T02:45:00+99:99",
        "2026-11-24T02:45:00Z\nSYNTHETIC_SECRET_MARKER",
        "x" * 1000,
        None,
    ],
)
def test_malformed_cutover_is_unknown_without_leaking_its_raw_value(raw, caplog):
    settings = SimpleNamespace(**{**VALUES, FIELDS[1]: raw})
    with caplog.at_level(logging.INFO):
        log_snapshot("worker", settings=settings)
    assert snapshot(settings=settings)[FIELDS[1]] is None
    assert "SYNTHETIC_SECRET_MARKER" not in caplog.text
    assert "x" * 100 not in caplog.text


def test_non_boolean_setting_is_unknown_not_a_truthy_grant():
    settings = SimpleNamespace(**{**VALUES, FIELDS[0]: "false"})
    assert snapshot(settings=settings)[FIELDS[0]] is None


def test_worker_ready_logs_only_resolved_six_fields(caplog, monkeypatch):
    from config.celery import log_source_purpose_configuration

    monkeypatch.setenv(FIELDS[0], "false")
    with override_settings(**VALUES, SECRET_KEY="SYNTHETIC_SECRET_MARKER"):
        with caplog.at_level(logging.INFO):
            log_source_purpose_configuration()
    records = [
        r.getMessage() for r in caplog.records if "LSA_SOURCE_PURPOSE_HEALTH " in r.getMessage()
    ]
    assert len(records) == 1
    assert json.loads(records[0].split("LSA_SOURCE_PURPOSE_HEALTH ", 1)[1]) == {
        "role": "worker",
        "values": VALUES,
    }
    assert "SYNTHETIC_SECRET_MARKER" not in caplog.text


def test_only_the_six_approved_operational_fields_can_be_read():
    assert set(FIELDS) == {
        "MESSAGE_LOYALTY_SOURCE_V2_ENABLED",
        "MESSAGE_LOYALTY_SOURCE_CUTOVER_AT",
        "MESSAGE_CANCELLATION_SOURCE_V2_ENABLED",
        "MESSAGE_CANCELLATION_SOURCE_CUTOVER_AT",
        "MESSAGE_EXTENSION_SOURCE_V2_ENABLED",
        "MESSAGE_EXTENSION_SOURCE_CUTOVER_AT",
    }


def test_extension_metadata_does_not_inherit_normal_or_loyalty_cutover():
    environment = {FIELDS[0]: "true", FIELDS[1]: EPOCH, "MESSAGE_SOURCE_CUTOVER_AT": EPOCH}
    values = snapshot(environment=environment)
    assert values["MESSAGE_EXTENSION_SOURCE_V2_ENABLED"] is False
    assert values["MESSAGE_EXTENSION_SOURCE_CUTOVER_AT"] == ""
    environment["MESSAGE_EXTENSION_SOURCE_CUTOVER_AT"] = "SYNTHETIC_SECRET_MARKER"
    assert snapshot(environment=environment)["MESSAGE_EXTENSION_SOURCE_CUTOVER_AT"] is None


def test_actual_beat_init_readback_is_isolated_and_has_no_provider_imports():
    result = run_isolated(
        """
import logging,sys
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
from celery.signals import beat_init
from scheduler_runtime.app import app
beat_init.send(sender=object())
assert app.fixups == []
assert 'django.conf' not in sys.modules
assert 'config.settings.production' not in sys.modules
assert 'apps.notifications.tasks' not in sys.modules
assert 'apps.payments.tasks' not in sys.modules
""",
        {
            **{key: "true" if isinstance(value, bool) else value for key, value in VALUES.items()},
            "SAMA_API_KEY": "SYNTHETIC_SECRET_MARKER",
        },
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if "LSA_SOURCE_PURPOSE_HEALTH " in line]
    assert len(lines) == 1
    assert json.loads(lines[0].split("LSA_SOURCE_PURPOSE_HEALTH ", 1)[1]) == {
        "role": "beat",
        "values": VALUES,
    }
    assert "SYNTHETIC_SECRET_MARKER" not in result.stdout + result.stderr
