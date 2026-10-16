"""Template-safe authentication capability flags."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest

from .access_copy import access_copy


def authentication_options(_request: HttpRequest) -> dict[str, object]:
    """Expose providers only once their server-side credentials are configured."""

    return {
        "guest_access_copy": access_copy(),
        "google_sign_in_enabled": settings.GOOGLE_SIGN_IN_ENABLED,
        "apple_sign_in_enabled": settings.APPLE_SIGN_IN_ENABLED,
    }
