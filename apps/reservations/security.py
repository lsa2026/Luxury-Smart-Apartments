"""Session ownership, masking, and cache-backed abuse controls."""

import secrets
from hashlib import sha256

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest
from django.utils.crypto import constant_time_compare, salted_hmac

SESSION_MARKER_KEY = "booking_session_marker"


def hash_session_marker(marker: str) -> str:
    return salted_hmac("booking-session-owner.v1", marker).hexdigest()


def session_key_hash(request: HttpRequest) -> str:
    marker = request.session.get(SESSION_MARKER_KEY)
    if not marker:
        marker = secrets.token_urlsafe(24)
        request.session[SESSION_MARKER_KEY] = marker
    return hash_session_marker(marker)


def session_owns(request: HttpRequest, expected_hash: str) -> bool:
    return constant_time_compare(session_key_hash(request), expected_hash)


def is_rate_limited(
    request: HttpRequest,
    *,
    scope: str,
    requests: int,
    window: int,
) -> bool:
    marker = request.session.get(SESSION_MARKER_KEY)
    if not marker:
        marker = secrets.token_urlsafe(24)
        request.session[SESSION_MARKER_KEY] = marker
    remote_address = request.META.get("REMOTE_ADDR", "")
    digest = sha256(f"{settings.SECRET_KEY}:{scope}:{marker}:{remote_address}".encode()).hexdigest()
    key = f"booking-rate:v1:{scope}:{digest}"
    if cache.add(key, 1, timeout=window):
        return False
    try:
        attempts = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window)
        attempts = 1
    return attempts > requests


def mask_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "****"
    visible = local[:1]
    return f"{visible}***@{domain}"


def mask_phone(value: str) -> str:
    return f"****{value[-4:]}" if len(value) >= 4 else "****"
