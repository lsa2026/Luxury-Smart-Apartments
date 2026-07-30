"""Allowlisted, privacy-safe audit recording."""

from collections.abc import Mapping
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils.crypto import salted_hmac
from django.utils.html import strip_tags

from apps.notifications.models import AuditLog

ALLOWED_METADATA_KEYS = {
    "fields",
    "count",
    "report",
    "status",
    "source",
    "dry_run",
    "notification_type",
}


def hash_ip(raw_ip: str) -> str | None:
    if not raw_ip:
        return None
    return salted_hmac("audit-ip.v1", raw_ip, secret=settings.SECRET_KEY).hexdigest()


def user_agent_family(value: str) -> str:
    normalized = value.casefold()
    for family in ("edge", "edg/", "chrome", "firefox", "safari"):
        if family in normalized:
            return "Edge" if family in {"edge", "edg/"} else family.title()
    return "Other" if value else ""


def sanitize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        if key not in ALLOWED_METADATA_KEYS:
            continue
        if isinstance(value, bool | int | float) or value is None:
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = strip_tags(value)[:200]
        elif isinstance(value, list | tuple):
            clean[key] = [strip_tags(str(item))[:80] for item in value[:30]]
    return clean


def record_audit(
    *,
    action: str,
    object_type: str,
    object_reference: str,
    summary: str,
    request: HttpRequest | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AuditLog:
    actor = None
    ip_digest = None
    agent_family = ""
    if request is not None:
        actor = request.user if request.user.is_authenticated else None
        ip_digest = hash_ip(request.META.get("REMOTE_ADDR", ""))
        agent_family = user_agent_family(request.META.get("HTTP_USER_AGENT", ""))
    return AuditLog.objects.create(
        actor_user=actor,
        action=action[:100],
        object_type=object_type[:100],
        object_reference=str(object_reference)[:100],
        summary=strip_tags(summary)[:300],
        metadata=sanitize_metadata(metadata),
        ip_hash=ip_digest,
        user_agent_family=agent_family,
    )
