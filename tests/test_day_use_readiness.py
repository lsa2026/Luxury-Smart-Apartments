"""Deployment gates must not send or expose credentials."""

from io import StringIO
from unittest.mock import Mock

import pytest
from django.core.management import CommandError, call_command
from django.db import connection


def test_disabled_configuration_check_needs_no_database_or_network(settings, monkeypatch):
    settings.DAY_USE08_ENABLED = False
    monkeypatch.setattr(connection.introspection, "table_names", Mock(side_effect=AssertionError))
    output = StringIO()
    call_command("check_day_use08_readiness", stdout=output)
    assert "disabled; no dispatch" in output.getvalue()


def test_worker_build_refuses_incomplete_schema(settings, monkeypatch):
    settings.DAY_USE08_ENABLED = False
    monkeypatch.setattr(connection.introspection, "table_names", lambda: [])
    clock = iter([0, 61])
    monkeypatch.setattr(
        "apps.reservations.management.commands.check_day_use08_readiness.time.monotonic",
        lambda: next(clock),
    )
    with pytest.raises(CommandError, match="schema not ready"):
        call_command("check_day_use08_readiness", wait_for_schema=True, stdout=StringIO())


@pytest.mark.django_db
def test_worker_schema_check_accepts_committed_migrations(settings):
    settings.DAY_USE08_ENABLED = False
    output = StringIO()
    call_command("check_day_use08_readiness", wait_for_schema=True, stdout=output)
    assert "schema present" in output.getvalue() and "disabled" in output.getvalue()


@pytest.mark.django_db
def test_worker_waits_for_manual_address_migration(settings, monkeypatch):
    from django.db.migrations.recorder import MigrationRecorder

    settings.DAY_USE08_ENABLED = False
    applied = set(MigrationRecorder(connection).applied_migrations())
    applied.remove(("reservations", "0032_manualbookingdraft_billing_address"))
    monkeypatch.setattr(MigrationRecorder, "applied_migrations", lambda self: applied)
    clock = iter([0, 61])
    monkeypatch.setattr(
        "apps.reservations.management.commands.check_day_use08_readiness.time.monotonic",
        lambda: next(clock),
    )
    with pytest.raises(CommandError, match="schema not ready"):
        call_command("check_day_use08_readiness", wait_for_schema=True, stdout=StringIO())


@pytest.mark.parametrize(
    "service_id,expects_django",
    [
        ("srv-dal662tbedkc73bdlghg", True),
        ("srv-dal662tbedkc73bdlgj0", False),
    ],
)
def test_build_gate_preserves_dispatch_only_scheduler(tmp_path, service_id, expects_django):
    import os
    import subprocess
    from pathlib import Path

    if os.name == "nt":
        pytest.skip("Native build shell runs on the Linux deployment engine")
    command_log = tmp_path / "commands.log"
    for name in ("pip", "python"):
        program = tmp_path / name
        program.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$TEST_COMMAND_LOG"\n', encoding="utf-8"
        )
        program.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "RENDER_SERVICE_ID": service_id,
        "TEST_COMMAND_LOG": str(command_log),
    }
    result = subprocess.run(
        ["bash", str(Path(__file__).resolve().parents[1] / "build-worker.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert ("check_day_use08_readiness" in command_log.read_text()) is expects_django
