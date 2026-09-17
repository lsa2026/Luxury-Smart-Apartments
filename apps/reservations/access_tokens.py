"""Short-lived, single-use links for booking-management recovery."""

from __future__ import annotations

from hashlib import sha256

from django.conf import settings
from django.core import signing
from django.core.cache import cache


ACCESS_LINK_SALT = "reservations.management-access-link.v1"
ACCESS_LINK_MAX_AGE_SECONDS = 30 * 60


def make_access_link_token(public_reference: str, email: str) -> str:
    return signing.dumps(
        {"reference": public_reference, "email": email.strip().casefold()},
        salt=ACCESS_LINK_SALT,
    )


def consume_access_link_token(token: str) -> tuple[str, str] | None:
    """Return the booking identity once, or ``None`` for every failure."""
    try:
        payload = signing.loads(
            token,
            salt=ACCESS_LINK_SALT,
            max_age=getattr(
                settings,
                "BOOKING_MANAGEMENT_ACCESS_LINK_MAX_AGE_SECONDS",
                ACCESS_LINK_MAX_AGE_SECONDS,
            ),
        )
    except (signing.BadSignature, signing.SignatureExpired, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    reference = payload.get("reference")
    email = payload.get("email")
    if not isinstance(reference, str) or not reference or not isinstance(email, str) or not email:
        return None

    digest = sha256(token.encode()).hexdigest()
    key = f"reservation-access-link:v1:{digest}"
    ttl = getattr(
        settings,
        "BOOKING_MANAGEMENT_ACCESS_LINK_MAX_AGE_SECONDS",
        ACCESS_LINK_MAX_AGE_SECONDS,
    )
    if not cache.add(key, 1, timeout=ttl):
        return None
    return reference, email
