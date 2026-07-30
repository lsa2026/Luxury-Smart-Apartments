"""Fast public health probes without Hostaway or email calls."""

import secrets
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse


def live(request: object) -> JsonResponse:
    del request
    return JsonResponse({"status": "ok"})


def readiness_status() -> tuple[bool, dict[str, str]]:
    checks: dict[str, str] = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"

    key = f"health:ready:{secrets.token_hex(8)}"
    try:
        cache.set(key, "ok", timeout=10)
        checks["cache"] = "ok" if cache.get(key) == "ok" else "unavailable"
        cache.delete(key)
    except Exception:
        checks["cache"] = "unavailable"

    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        checks["migrations"] = "ok" if not pending else "pending"
    except Exception:
        checks["migrations"] = "unavailable"

    if settings.REDIS_URL:
        try:
            from redis import Redis

            redis_client = Redis.from_url(
                settings.REDIS_URL,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            checks["redis"] = "ok" if redis_client.ping() else "unavailable"
        except Exception:
            checks["redis"] = "unavailable"

    checks["configuration"] = (
        "ok" if settings.SECRET_KEY and settings.SITE_BASE_URL else "unavailable"
    )
    ready = all(value == "ok" for value in checks.values())
    return ready, checks


def ready(request: Any) -> JsonResponse:
    del request
    is_ready, checks = readiness_status()
    return JsonResponse(
        {"status": "ok" if is_ready else "unavailable", "checks": checks},
        status=200 if is_ready else 503,
    )
