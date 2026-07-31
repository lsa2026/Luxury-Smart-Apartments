"""Small, dependency-free browser security header middleware."""

import secrets
from collections.abc import Callable

from django.conf import settings
from django.db import DatabaseError
from django.db.models import F
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from .models import LegacyRedirect


class LegacyRedirectMiddleware:
    """Resolve approved exact legacy paths after Django returns a 404."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if (
            response.status_code != 404
            or request.method not in {"GET", "HEAD"}
            or request.path.startswith(LegacyRedirect.PROTECTED_PREFIXES)
        ):
            return response
        try:
            redirect = LegacyRedirect.objects.only(
                "pk",
                "destination_path",
                "redirect_type",
            ).get(source_path=request.path, is_active=True)
        except (LegacyRedirect.DoesNotExist, DatabaseError):
            return response

        try:
            LegacyRedirect.objects.filter(pk=redirect.pk).update(
                hit_count=F("hit_count") + 1,
                last_hit_at=timezone.now(),
            )
        except DatabaseError:
            pass

        if redirect.redirect_type == LegacyRedirect.RedirectType.GONE:
            request._disable_google_integrations = True
            return render(request, "errors/410.html", status=410)
        redirect_response = HttpResponse(status=redirect.redirect_type)
        redirect_response["Location"] = redirect.destination_path
        return redirect_response


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
        google_allowed = (
            settings.GOOGLE_INTEGRATIONS_ENABLED
            and not request.path.startswith(("/admin/", "/health/", "/integrations/"))
            and not getattr(request, "_disable_google_integrations", False)
        )
        script_sources = ["'self'", f"'nonce-{request.csp_nonce}'"]
        connect_sources = ["'self'"]
        frame_sources = ["'self'"]
        if google_allowed and (
            settings.GOOGLE_TAG_MANAGER_ENABLED or settings.GOOGLE_ANALYTICS_ENABLED
        ):
            script_sources.append("https://www.googletagmanager.com")
        if google_allowed and settings.GOOGLE_ANALYTICS_ENABLED:
            connect_sources.extend(
                [
                    "https://www.google-analytics.com",
                    "https://region1.google-analytics.com",
                ]
            )
        if google_allowed and settings.GOOGLE_TAG_MANAGER_ENABLED:
            frame_sources.append("https://www.googletagmanager.com")
        if google_allowed and settings.GOOGLE_ADS_ENABLED:
            connect_sources.extend(
                [
                    "https://www.googleadservices.com",
                    "https://googleads.g.doubleclick.net",
                ]
            )
        response.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; base-uri 'self'; form-action 'self'; "
                "frame-ancestors 'none'; object-src 'none'; "
                f"img-src 'self' data: {image_sources}; "
                f"style-src {style_sources}; "
                f"script-src {' '.join(script_sources)}; "
                f"connect-src {' '.join(connect_sources)}; "
                f"frame-src {' '.join(frame_sources)}"
            ),
        )
        response.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        return response
