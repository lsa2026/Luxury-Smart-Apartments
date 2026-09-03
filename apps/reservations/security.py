"""Session ownership, masking, and cache-backed abuse controls."""

import secrets
from hashlib import sha256

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

SESSION_MARKER_KEY = "booking_session_marker"
MANAGED_RESERVATIONS_KEY = "managed_reservations"


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


def _management_key(public_reference: str) -> str:
    return salted_hmac("reservation-management.v1", public_reference).hexdigest()


def grant_reservation_access(request: HttpRequest, public_reference: str) -> None:
    """Grant time-limited access without retaining guest data in the session."""
    request.session.cycle_key()
    now = int(timezone.now().timestamp())
    grants = request.session.get(MANAGED_RESERVATIONS_KEY, {})
    if not isinstance(grants, dict):
        grants = {}
    grants = {
        key: expiry
        for key, expiry in grants.items()
        if isinstance(expiry, int) and expiry > now
    }
    grants[_management_key(public_reference)] = (
        now + settings.BOOKING_MANAGEMENT_SESSION_TTL_SECONDS
    )
    request.session[MANAGED_RESERVATIONS_KEY] = grants
    request.session.set_expiry(settings.BOOKING_MANAGEMENT_SESSION_TTL_SECONDS)


def session_can_manage(request: HttpRequest, public_reference: str) -> bool:
    grants = request.session.get(MANAGED_RESERVATIONS_KEY, {})
    if not isinstance(grants, dict):
        return False
    now = int(timezone.now().timestamp())
    key = _management_key(public_reference)
    expiry = grants.get(key)
    if not isinstance(expiry, int) or expiry <= now:
        if key in grants:
            grants.pop(key, None)
            request.session[MANAGED_RESERVATIONS_KEY] = grants
        return False
    return True


def revoke_reservation_access(request: HttpRequest) -> None:
    request.session.pop(MANAGED_RESERVATIONS_KEY, None)
    request.session.pop(SESSION_MARKER_KEY, None)
    request.session.cycle_key()


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


def is_ip_rate_limited(
    request: HttpRequest,
    *,
    scope: str,
    requests: int,
    window: int,
) -> bool:
    """Rate-limit credential checks even when a client clears its session cookie."""
    remote_address = request.META.get("REMOTE_ADDR", "")
    digest = sha256(f"{settings.SECRET_KEY}:{scope}:{remote_address}".encode()).hexdigest()
    key = f"booking-ip-rate:v1:{scope}:{digest}"
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
