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
