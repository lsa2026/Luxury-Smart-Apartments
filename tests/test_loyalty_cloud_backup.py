"""Offline cloud-transport contracts; no production rows, credentials or network."""

import copy
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache

from apps.loyalty.backup import BackupError, seal, validate
from apps.loyalty.cloud_backup import (
    HEALTH_KEY,
    PROJECT,
    CloudMirror,
    connection_options,
    mark_failure,
    mark_success,
)
from apps.loyalty.services import transfer
from apps.notifications.models import Notification
from tests.test_loyalty_stage10 import local_program as _local_fixture
from tests.test_loyalty_stage10 import sequence as _sequence_fixture
from tests.test_loyalty_stage10 import snapshot

pytestmark = pytest.mark.django_db
DSN = f"postgresql://postgres:fictional-secret@db.{PROJECT}.supabase.co:5432/postgres"
SOURCE = "fictional-local-stage-ten"


@pytest.fixture(autouse=True)
def local_program(settings, monkeypatch):
    return _local_fixture.__wrapped__(settings, monkeypatch)


@pytest.fixture
def sequence(local_program):
    return _sequence_fixture.__wrapped__(local_program)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        state = self.connection.state
        self.connection.store.queries.append(query)
        if query.startswith("SET TRANSACTION"):
            return
        if query.startswith("SELECT payload, project_ref"):
            self.result = [(state["manifest"], PROJECT, 1)]
        elif query.startswith("SELECT key, payload"):
            table = query.split("FROM ")[1].split()[0].split(".")[1]
            self.result = sorted(copy.deepcopy(state[table]).items())
        elif query.startswith("SELECT payload FROM"):
            self.result = [(copy.deepcopy(state["status"]),)]
        elif query.startswith("UPDATE "):
            table = query.split()[1].split(".")[1]
            state[table] = copy.deepcopy(params[0].obj)
        else:
            raise AssertionError(query)

    def fetchone(self):
        return self.result[0] if self.result else None

    def __iter__(self):
        return iter(self.result)

    def executemany(self, query, rows):
        table = query.split("INTO ")[1].split()[0].split(".")[1]
        self.connection.store.queries.append(query)
        for key, value in rows:
            self.connection.state[table][key] = copy.deepcopy(value.obj)
            if table == self.connection.store.fail_table:
                raise RuntimeError("fictional-secret must not be printed")


class FakeConnection:
    def __init__(self, store):
        self.store = store
        self.state = copy.deepcopy(store.state)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *args):
        if exc_type is None:
            if self.store.fail_commit:
                raise RuntimeError("fictional-secret commit failure")
            self.store.state = self.state
        return False

    def cursor(self):
        return FakeCursor(self)


class FakeStore:
    def __init__(self):
        self.state = {
            "members": {},
            "referrals": {},
            "ledger": {},
            "context": {},
            "manifest": None,
            "status": {"status": "never_run", "last_success": None},
        }
        self.queries = []
        self.fail_table = ""
        self.fail_commit = False

    def connect(self, **options):
        assert options["sslmode"] == "verify-full"
        assert options["prepare_threshold"] is None
        return FakeConnection(self)


@pytest.fixture
def cloud(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr("apps.loyalty.cloud_backup.psycopg.connect", store.connect)
    cache.delete(HEALTH_KEY)
    return CloudMirror(dsn=DSN, project=PROJECT, source_id=SOURCE), store


@pytest.mark.parametrize(
    "dsn",
    [
        DSN,
        f"postgresql://postgres.{PROJECT}:fictional-secret@aws-1-ap-south-1.pooler.supabase.com:6543/postgres",
    ],
)
def test_only_approved_project_encrypted_connection(dsn):
    options = connection_options(dsn, project=PROJECT)
    assert options["sslrootcert"] == "system"
    assert options["sslmode"] == "verify-full"
    assert options["connect_timeout"] == 10


@pytest.mark.parametrize(
    "dsn",
    [
        DSN.replace(PROJECT, "wrongproject"),
        DSN + "?sslmode=require",
        DSN + "?hostaddr=127.0.0.1",
        DSN + "?options=-c%20search_path=public",
        DSN.replace("/postgres", "/wrongdatabase"),
        "service=external",
        "invalid-connection",
        "postgresql://postgres:secret@aws-1-ap-south-1.pooler.supabase.com:5432/postgres",
    ],
)
def test_wrong_destination_or_security_downgrade_never_connects(dsn):
    with patch("apps.loyalty.cloud_backup.psycopg.connect") as connect:
        with pytest.raises(BackupError):
            CloudMirror(dsn=dsn, project=PROJECT, source_id=SOURCE)
        connect.assert_not_called()


def test_first_copy_readback_then_same_tables_update(sequence, cloud):
    mirror, store = cloud
    assert mirror.read() is None
    good = snapshot()
    assert mirror.update(good) == validate(good)
    assert mirror.read() == good
    mirror.update(good)
    assert len(store.state["ledger"]) == 11
    a, _, _, _, members = sequence
    transfer(a, "TEST-CLOUD-SECOND", members[-1].number, "1.00")
    fresh = snapshot()
    mirror.update(fresh)
    assert mirror.read() == fresh
    assert len(store.state["ledger"]) == 13
    assert cache.get(HEALTH_KEY)["digest"] == fresh["digest"]
    assert any("FOR UPDATE" in q for q in store.queries)
    assert not any(q.startswith(("DELETE", "CREATE", "GRANT", "REVOKE")) for q in store.queries)


@pytest.mark.parametrize("fail_at", ["members", "referrals", "ledger", "context", "commit"])
def test_failed_partial_update_rolls_back_and_redacts(sequence, cloud, fail_at):
    mirror, store = cloud
    good = snapshot()
    mirror.update(good)
    before = copy.deepcopy(store.state)
    a, _, _, _, members = sequence
    transfer(a, "TEST-CLOUD-FAIL", members[-1].number, "1.00")
    if fail_at == "commit":
        store.fail_commit = True
    else:
        store.fail_table = fail_at
    with pytest.raises(BackupError) as error:
        mirror.update(snapshot())
    assert error.value.code == "cloud_io_failed"
    assert "fictional-secret" not in str(error.value)
    assert error.value.__cause__ is None
    assert store.state == before
    assert cache.get(HEALTH_KEY)["digest"] == good["digest"]


def test_missing_history_or_stale_snapshot_does_not_replace_copy(sequence, cloud):
    mirror, store = cloud
    good = snapshot()
    mirror.update(good)
    before = copy.deepcopy(store.state)
    empty = copy.deepcopy(good)
    empty["records"] = []
    with pytest.raises(BackupError, match="."):
        mirror.update(seal(empty))
    stale = copy.deepcopy(good)
    from datetime import datetime

    stale["captured_at"] = (
        datetime.fromisoformat(good["captured_at"]) - timedelta(days=1)
    ).isoformat()
    with pytest.raises(BackupError) as error:
        mirror.update(seal(stale))
    assert error.value.code == "stale_snapshot"
    assert store.state == before


def test_corrupt_destination_or_different_source_rejected(sequence, cloud):
    mirror, store = cloud
    mirror.update(snapshot())
    wrong = CloudMirror(dsn=DSN, project=PROJECT, source_id="wrong-source")
    with pytest.raises(BackupError) as error:
        wrong.read()
    assert error.value.code == "different_source"
    store.state["members"]["invalid-key"] = next(iter(store.state["members"].values()))
    with pytest.raises(BackupError) as error:
        mirror.read()
    assert error.value.code == "invalid_cloud_record_key"


def test_source_guard_prevents_test_database_upload(cloud):
    mirror, store = cloud
    with pytest.raises(BackupError) as error:
        mirror.run()
    assert error.value.code == "cloud_requires_production_source"
    assert not store.queries


def test_failure_preserves_last_success_and_single_admin_notice(sequence, cloud):
    mirror, store = cloud
    mirror.update(snapshot())
    last = store.state["status"]["last_success"]
    mirror.failed("cloud_io_failed")
    assert store.state["status"]["last_success"] == last
    mark_failure("cloud_io_failed")
    mark_failure("cloud_io_failed")
    assert Notification.objects.filter(related_object_type="LoyaltyBackup").count() == 1
    assert cache.get(HEALTH_KEY)["last_success"] == last
    mark_success()
    assert Notification.objects.get(related_object_type="LoyaltyBackup").status == "archived"


def test_cache_failure_cannot_hide_backup_failure_notice():
    with (
        patch("apps.loyalty.cloud_backup.cache.get", side_effect=RuntimeError),
        patch("apps.loyalty.cloud_backup.cache.set", side_effect=RuntimeError),
    ):
        mark_failure("cloud_io_failed")
    assert Notification.objects.filter(related_object_type="LoyaltyBackup").exists()


def test_cloud_task_never_uses_local_fallback(settings):
    settings.LOYALTY_BACKUP_ENABLED = True
    settings.LOYALTY_BACKUP_LOCAL_REHEARSAL = False
    settings.LOYALTY_BACKUP_DATABASE_URL = DSN
    settings.LOYALTY_BACKUP_PROJECT_REF = PROJECT
    settings.LOYALTY_BACKUP_SOURCE_ID = SOURCE
    from apps.loyalty.tasks import daily_backup_task

    with (
        patch(
            "apps.loyalty.cloud_backup.CloudMirror.run",
            return_value={
                "counts": {"loyalty.member": 4},
                "program_balance": "233.50",
            },
        ) as run,
        patch("apps.loyalty.tasks.LocalMirror.run") as local,
    ):
        assert daily_backup_task()["status"] == "succeeded"
        run.assert_called_once()
        local.assert_not_called()


def test_missing_cloud_connection_notifies_admin_without_reading_source(settings):
    from django.core.exceptions import ImproperlyConfigured

    from apps.loyalty.tasks import daily_backup_task

    settings.LOYALTY_BACKUP_ENABLED = True
    settings.LOYALTY_BACKUP_LOCAL_REHEARSAL = False
    settings.LOYALTY_BACKUP_DATABASE_URL = ""
    with patch("apps.loyalty.cloud_backup.capture") as source:
        with pytest.raises(ImproperlyConfigured):
            daily_backup_task.run()
        source.assert_not_called()
    assert Notification.objects.filter(related_object_type="LoyaltyBackup").exists()
