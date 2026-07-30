"""Small, dependency-free browser security header middleware."""

import secrets
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse


class SecurityHeadersMiddleware:
    """Apply a conservative baseline CSP and Permissions Policy."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.csp_nonce = secrets.token_urlsafe(18)
        response = self.get_response(request)
        image_sources = " ".join(
            source
            for source in settings.HOSTAWAY_IMAGE_CSP_SOURCES
            if source.startswith("https://") and ";" not in source
        )
        style_sources = "'self' 'unsafe-inline'" if request.path.startswith("/admin/") else "'self'"
        response.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; base-uri 'self'; form-action 'self'; "
                "frame-ancestors 'none'; object-src 'none'; "
                f"img-src 'self' data: {image_sources}; "
                f"style-src {style_sources}; "
                f"script-src 'self' 'nonce-{request.csp_nonce}'"
            ),
        )
        response.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        return response
