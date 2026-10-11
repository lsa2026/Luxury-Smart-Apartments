"""Approved private Postgres mirror. No runtime DDL, grants, providers or public API.

Connection credentials are supplied through server secrets only. This adapter
does not activate itself, publish code, or claim that an empty destination is a
successful backup. One transaction advances data, manifest and success status.
"""

import logging
import re
from collections import defaultdict
from contextlib import contextmanager

import psycopg
from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.utils import timezone
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from .backup import (
    MAX_ROWS,
    BackupError,
    LocalMirror,
    _reject,
    capture,
    compare,
    validate,
)

PROJECT = "rsqgwepnowpnpealrzbn"
SCHEMA = "lsa_loyalty_backup"
TABLES = (*LocalMirror.TABLES.values(), "context")
HEALTH_KEY = "loyalty:external-backup-health:v1"
logger = logging.getLogger(__name__)


def connection_options(dsn, *, project, certificate="system"):
    """Validate destination BEFORE reading private production rows or connecting."""
    if project != PROJECT or not dsn:
        _reject("approved_cloud_connection_required")
    try:
        options = conninfo_to_dict(dsn)
    except Exception:
        _reject("invalid_cloud_connection")
    allowed = {"host", "port", "dbname", "user", "password", "sslmode", "sslrootcert"}
    if set(options) - allowed:
        _reject("unreviewed_cloud_connection_option")
    host, user = options.get("host", ""), options.get("user", "")
    direct = host == f"db.{PROJECT}.supabase.co"
    pooled = bool(re.fullmatch(r"aws-\d+-ap-south-1\.pooler\.supabase\.com", host))
    if not direct and not (pooled and user.endswith(f".{PROJECT}")):
        _reject("cloud_destination_mismatch")
    if (
        options.get("dbname") != "postgres"
        or options.get("port", "5432") not in {"5432", "6543"}
        or not user
        or not options.get("password")
    ):
        _reject("invalid_cloud_connection")
    if options.get("sslmode", "verify-full") != "verify-full":
        _reject("cloud_certificate_verification_required")
    # A pinned server setting takes precedence; never downgrade TLS for a failed certificate.
    options.update(
        sslmode="verify-full",
        sslrootcert=certificate or "system",
        connect_timeout=10,
        prepare_threshold=None,
        application_name="lsa-loyalty-backup",
        options="-c statement_timeout=30000 -c lock_timeout=5000",
        gssencmode="disable",
    )
    return options


class CloudMirror:
    def __init__(self, *, dsn, project, source_id, certificate="system"):
        self.options = connection_options(dsn, project=project, certificate=certificate)
        if not isinstance(source_id, str) or not 3 <= len(source_id) <= 100:
            _reject("source_identity_required")
        self.source_id = source_id

    @classmethod
    def configured(cls):
        return cls(
            dsn=settings.LOYALTY_BACKUP_DATABASE_URL,
            project=settings.LOYALTY_BACKUP_PROJECT_REF,
            source_id=settings.LOYALTY_BACKUP_SOURCE_ID,
            certificate=settings.LOYALTY_BACKUP_SSL_ROOT_CERT,
        )

    @contextmanager
    def _connect(self):
        # Redact libpq/provider exceptions, including credentials, SQL and private row values.
        try:
            with psycopg.connect(**self.options) as db:
                yield db
        except BackupError:
            raise
        except Exception:
            raise BackupError(
                "تعذر الاتصال بالنسخة الخارجية أو حفظها.", code="cloud_io_failed"
            ) from None

    def _read(self, cursor, *, lock=False):
        cursor.execute(
            f"SELECT payload, project_ref, format_version FROM {SCHEMA}.manifest "
            "WHERE id=1" + (" FOR UPDATE" if lock else "")
        )
        header = cursor.fetchone()
        if header is None or header[1:] != (PROJECT, 1):
            _reject("cloud_schema_not_provisioned")
        records = []
        for table in TABLES:
            cursor.execute(f"SELECT key, payload FROM {SCHEMA}.{table} ORDER BY key")
            for key, row in cursor:
                if not isinstance(row, dict) or key != f"{row.get('model')}:{row.get('pk')}":
                    _reject("invalid_cloud_record_key")
                expected = LocalMirror.TABLES.get(row.get("model"), "context")
                if expected != table:
                    _reject("invalid_cloud_record_partition")
                records.append(row)
                if len(records) > MAX_ROWS:
                    _reject("snapshot_row_limit")
        if header[0] is None:
            if records:
                _reject("missing_mirror_manifest")
            return None
        snapshot = {
            **header[0],
            "records": sorted(records, key=lambda r: (r["model"], str(r["pk"]))),
        }
        validate(snapshot)
        if snapshot["source_id"] != self.source_id:
            _reject("different_source")
        return snapshot

    def read(self):
        with self._connect() as db, db.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return self._read(cursor)

    def update(self, snapshot):
        validate(snapshot)
        if snapshot["source_id"] != self.source_id:
            _reject("different_source")
        rows = defaultdict(list)
        for row in snapshot["records"]:
            table = LocalMirror.TABLES.get(row["model"], "context")
            rows[table].append((f"{row['model']}:{row['pk']}", Jsonb(row)))
        with self._connect() as db, db.cursor() as cursor:
            # The pre-provisioned singleton locks even the FIRST update.
            previous = self._read(cursor, lock=True)
            compare(previous, snapshot)
            for table in TABLES:
                if rows[table]:
                    cursor.executemany(
                        f"INSERT INTO {SCHEMA}.{table} (key,payload) VALUES (%s,%s) "
                        "ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload",
                        rows[table],
                    )
            manifest = {key: value for key, value in snapshot.items() if key != "records"}
            cursor.execute(
                f"UPDATE {SCHEMA}.manifest SET payload=%s WHERE id=1", (Jsonb(manifest),)
            )
            recovered = self._read(cursor)
            result = validate(recovered)
            if recovered["digest"] != snapshot["digest"]:
                _reject("cloud_readback_mismatch")
            now = timezone.now().isoformat()
            status = {
                "status": "succeeded",
                "last_attempt": now,
                "last_success": now,
                "captured_at": snapshot["captured_at"],
                "digest": snapshot["digest"],
                "counts": result["counts"],
                "program_balance": result["program_balance"],
                "error_code": "",
            }
            cursor.execute(f"UPDATE {SCHEMA}.status SET payload=%s WHERE id=1", (Jsonb(status),))
        # Only record success locally AFTER destination commit has succeeded.
        remember_health(status)
        return result

    def failed(self, code):
        try:
            with self._connect() as db, db.cursor() as cursor:
                cursor.execute(f"SELECT payload FROM {SCHEMA}.status WHERE id=1 FOR UPDATE")
                row = cursor.fetchone()
                state = row[0] if row else {"last_success": None}
                state.update(
                    status="failed", last_attempt=timezone.now().isoformat(), error_code=code
                )
                cursor.execute(f"UPDATE {SCHEMA}.status SET payload=%s WHERE id=1", (Jsonb(state),))
        except BackupError:
            pass  # Source-side alert survives a destination outage.

    def run(self):
        if connections["default"].vendor != "postgresql":
            _reject("cloud_requires_production_source")
        try:
            snapshot = capture(source_id=self.source_id)
            return self.update(snapshot)
        except Exception as exc:
            code = exc.code if isinstance(exc, BackupError) else "source_capture_failed"
            self.failed(code)
            raise BackupError("تعذر إثبات اكتمال تحديث النسخة الخارجية.", code=code) from None


def remember_health(state):
    try:
        cache.set(HEALTH_KEY, state, timeout=3 * 24 * 60 * 60)
    except Exception:
        pass  # A successful destination commit is not undone by cache unavailability.


def mark_failure(code):
    from apps.notifications.models import Notification

    try:
        state = cache.get(HEALTH_KEY) or {"last_success": None}
    except Exception:
        state = {"last_success": None}
    state.update(status="failed", error_code=code, last_attempt=timezone.now().isoformat())
    remember_health(state)
    try:
        _save_failure_notice(Notification)
    except Exception:
        logger.error("Loyalty backup failed; administration notice could not be saved.")


def _save_failure_notice(Notification):
    Notification.objects.update_or_create(
        idempotency_key="loyalty-external-backup-failed",
        defaults={
            "notification_type": Notification.Type.SYSTEM_WARNING,
            "audience_type": Notification.Audience.ADMIN,
            "status": Notification.Status.ACTIVE,
            "severity": Notification.Severity.ERROR,
            "is_read": False,
            "title_ar": "تعذر تحديث النسخة الخارجية لبرنامج الولاء",
            "title_en": "Loyalty external backup update failed",
            "message_ar": "تحقق من الربط وسجل المهمة. لا تعتبر النسخة محدثة حتى نجاح قراءة التحقق.",
            "message_en": "Check connection and job status. A successful readback is required.",
            "related_object_type": "LoyaltyBackup",
            "related_object_reference": PROJECT,
            "action_url": "/admin/operations/hub/",
        },
    )


def mark_success():
    from apps.notifications.models import Notification

    try:
        Notification.objects.filter(idempotency_key="loyalty-external-backup-failed").update(
            status=Notification.Status.ARCHIVED
        )
    except Exception:
        logger.warning("Loyalty backup committed; previous administration notice remains open.")
