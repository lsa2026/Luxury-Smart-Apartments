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
        public_map_page = bool(getattr(request, "_public_map_enabled", False))
        property_admin_map = request.path.startswith("/admin/properties/property/")
        location_map_page = public_map_page or property_admin_map
        image_sources = " ".join(
            source
            for source in settings.HOSTAWAY_IMAGE_CSP_SOURCES
            if source.startswith("https://") and ";" not in source
        )
        if location_map_page:
            image_sources = f"{image_sources} https://tile.openstreetmap.org"
        style_sources = (
            "'self' 'unsafe-inline'"
            if request.path.startswith("/admin/") or public_map_page
            else "'self'"
        )
        google_allowed = (
            settings.GOOGLE_INTEGRATIONS_ENABLED
            and not request.path.startswith(("/admin/", "/health/", "/integrations/"))
            and not getattr(request, "_disable_google_integrations", False)
        )
        script_sources = ["'self'", f"'nonce-{request.csp_nonce}'"]
        connect_sources = ["'self'"]
        frame_sources = ["'self'"]
        google_image_sources: list[str] = []
        if getattr(request, "_public_osm_embed", False):
            frame_sources.append("https://www.openstreetmap.org")
        form_action_sources = ["'self'"]
        font_sources = ["'self'"]
        hyperpay_page = settings.HYPERPAY_ENABLED and request.path.startswith("/payments/hyperpay/")
        if hyperpay_page:
            script_sources.append(settings.HYPERPAY_WIDGET_ORIGIN)
            connect_sources.append(settings.HYPERPAY_WIDGET_ORIGIN)
            frame_sources.append(settings.HYPERPAY_WIDGET_ORIGIN)
            form_action_sources.append(settings.HYPERPAY_WIDGET_ORIGIN)
            font_sources.extend(["data:", settings.HYPERPAY_WIDGET_ORIGIN])
            image_sources = f"{image_sources} {settings.HYPERPAY_WIDGET_ORIGIN}"
            # COPYandPAY creates a runtime stylesheet and inserts its widget rules
            # into it. Keep this relaxation isolated to the TEST payment route.
            style_sources = f"{style_sources} 'unsafe-inline' {settings.HYPERPAY_WIDGET_ORIGIN}"
        if google_allowed and (
            settings.GOOGLE_TAG_MANAGER_ENABLED or settings.GOOGLE_ANALYTICS_ENABLED
        ):
            script_sources.append("https://www.googletagmanager.com")
            connect_sources.extend(
                [
                    "https://www.googletagmanager.com",
                    "https://www.google-analytics.com",
                    "https://region1.google-analytics.com",
                    "https://analytics.google.com",
                ]
            )
            google_image_sources.extend(
                [
                    "https://www.googletagmanager.com",
                    "https://www.google-analytics.com",
                ]
            )
        if google_allowed and settings.GOOGLE_TAG_MANAGER_ENABLED:
            frame_sources.append("https://www.googletagmanager.com")
        if google_allowed and settings.GOOGLE_ADS_ENABLED:
            script_sources.extend(
                [
                    "https://www.googleadservices.com",
                    "https://www.google.com",
                ]
            )
            connect_sources.extend(
                [
                    "https://www.googleadservices.com",
                    "https://www.google.com",
                    "https://googleads.g.doubleclick.net",
                ]
            )
            frame_sources.extend(
                [
                    "https://www.google.com",
                    "https://td.doubleclick.net",
                ]
            )
            google_image_sources.extend(
                [
                    "https://www.google.com",
                    "https://googleads.g.doubleclick.net",
                ]
            )
        if google_image_sources:
            image_sources = f"{image_sources} {' '.join(google_image_sources)}"
        response.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; base-uri 'self'; "
                f"form-action {' '.join(form_action_sources)}; "
                "frame-ancestors 'none'; object-src 'none'; "
                f"img-src 'self' data: {image_sources}; "
                f"style-src {style_sources}; "
                f"script-src {' '.join(script_sources)}; "
                f"connect-src {' '.join(connect_sources)}; "
                f"frame-src {' '.join(frame_sources)}; "
                f"font-src {' '.join(font_sources)}"
            ),
        )
        response.setdefault(
            "Permissions-Policy",
            (
                "camera=(), microphone=(), geolocation=(), payment=(self)"
                if hyperpay_page
                else "camera=(), microphone=(), geolocation=(), payment=()"
            ),
        )
        return response
