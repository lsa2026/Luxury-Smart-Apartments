import os

import pytest

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-production")
os.environ.setdefault("DJANGO_DEBUG", "False")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("DATABASE_URL", "sqlite:///test.sqlite3")


@pytest.fixture(scope="session", autouse=True)
def postgres_test_history_cleanup(django_db_setup):
    """Allow framework cleanup only; runtime history guards remain unchanged.

    PostgreSQL TRUNCATE triggers fire even for empty history tables. Django's
    transactional tests must flush their disposable database between cases.
    Temporarily disable user triggers inside that cleanup transaction, then
    immediately re-enable them. Never install this hook on a production database.
    """
    from django.db import connection

    if connection.vendor not in {"postgresql", "sqlite"}:
        yield
        return
    if connection.vendor == "postgresql" and not str(connection.settings_dict["NAME"]).startswith(
        "test_"
    ):
        raise RuntimeError("History cleanup is restricted to a disposable test database.")
    original = connection.ops.sql_flush

    def cleanup_sql(style, tables, *, reset_sequences=False, allow_cascade=False):
        statements = original(
            style, tables, reset_sequences=reset_sequences, allow_cascade=allow_cascade
        )
        if connection.vendor == "sqlite":
            if "loyalty_guestevent" not in tables:
                return statements
            # Disposable test cleanup only. Drop/recreate within the flush transaction;
            # ordinary runtime writes still exercise the real immutable triggers.
            before = [
                f"DROP TRIGGER IF EXISTS loyalty_event_{a}_immutable;" for a in ("update", "delete")
            ]
            after = [
                f"CREATE TRIGGER loyalty_event_{a.lower()}_immutable BEFORE {a} "
                "ON loyalty_guestevent BEGIN "
                "SELECT RAISE(ABORT, 'Guest history is append-only'); END;"
                for a in ("UPDATE", "DELETE")
            ]
            return [*before, *statements, *after]
        guarded = [
            t
            for t in (
                "loyalty_ledger",
                "loyalty_referral",
                "loyalty_referralrevision",
                "loyalty_guestevent",
            )
            if t in tables
        ]
        before = [f'ALTER TABLE "{t}" DISABLE TRIGGER USER;' for t in guarded]
        after = [f'ALTER TABLE "{t}" ENABLE TRIGGER USER;' for t in guarded]
        return [*before, *statements, *after]

    connection.ops.sql_flush = cleanup_sql
    try:
        yield
    finally:
        connection.ops.sql_flush = original
