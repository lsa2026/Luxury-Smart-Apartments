"""Cross-process synchronization lock for the PostgreSQL production database."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection

from .exceptions import HostawaySyncAlreadyRunningError

_PROPERTY_SYNC_LOCK_KEY = 1_279_619_408
_fallback_lock = threading.Lock()


@contextmanager
def hostaway_property_sync_lock() -> Iterator[None]:
    """Acquire a PostgreSQL advisory lock, with a test-safe local fallback."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [_PROPERTY_SYNC_LOCK_KEY])
            acquired = bool(cursor.fetchone()[0])
        if not acquired:
            raise HostawaySyncAlreadyRunningError(
                "Another Hostaway property sync is already running."
            )
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [_PROPERTY_SYNC_LOCK_KEY])
        return

    acquired = _fallback_lock.acquire(blocking=False)
    if not acquired:
        raise HostawaySyncAlreadyRunningError("Another Hostaway property sync is already running.")
    try:
        yield
    finally:
        _fallback_lock.release()
