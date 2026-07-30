"""Small, dependency-free browser security header middleware."""

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse


class SecurityHeadersMiddleware:
    """Apply a conservative baseline CSP and Permissions Policy."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        image_sources = " ".join(
            source
            for source in settings.HOSTAWAY_IMAGE_CSP_SOURCES
            if source.startswith("https://") and ";" not in source
        )
        response.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; base-uri 'self'; form-action 'self'; "
                "frame-ancestors 'none'; object-src 'none'; "
                f"img-src 'self' data: {image_sources}; "
                "style-src 'self' 'unsafe-inline'; "
                "script-src 'self'"
            ),
        )
        response.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        return response
