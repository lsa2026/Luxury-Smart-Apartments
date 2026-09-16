"""Template-safe authentication capability flags."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest


def authentication_options(_request: HttpRequest) -> dict[str, bool]:
    """Expose providers only once their server-side credentials are configured."""

    return {
        "google_sign_in_enabled": settings.GOOGLE_SIGN_IN_ENABLED,
        "apple_sign_in_enabled": settings.APPLE_SIGN_IN_ENABLED,
    }
