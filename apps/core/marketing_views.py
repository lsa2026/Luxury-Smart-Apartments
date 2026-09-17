"""Staff-only local diagnostics; no Google or Hostaway network calls."""

from typing import Any

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.properties.models import Property

from .checks import validate_google_configuration
from .marketing import PURCHASE_RECEIPT_TOKEN_SALT, sanitize_analytics_event
from .models import LegacyRedirect, MarketingEventReceipt
from .seo import property_structured_data, public_sitemap_entries, seo_diagnostics


def _allowed(request: HttpRequest, permission: str) -> None:
    if not (request.user.is_superuser or request.user.has_perm(permission)):
        raise PermissionDenied


def _mask_identifier(value: str) -> str:
    if not value:
        return "not configured"
    if len(value) <= 6:
        return "****"
    return f"{value[:3]}****{value[-3:]}"


def marketing_status() -> dict[str, Any]:
    errors = validate_google_configuration()
    return {
        "integrations_enabled": settings.GOOGLE_INTEGRATIONS_ENABLED,
        "gtm_configured": bool(settings.GOOGLE_TAG_MANAGER_CONTAINER_ID),
        "gtm_identifier": _mask_identifier(settings.GOOGLE_TAG_MANAGER_CONTAINER_ID),
        "ga4_configured": bool(settings.GOOGLE_ANALYTICS_MEASUREMENT_ID),
        "ga4_identifier": _mask_identifier(settings.GOOGLE_ANALYTICS_MEASUREMENT_ID),
        "ads_configured": bool(settings.GOOGLE_ADS_CONVERSION_ID),
        "ads_identifier": _mask_identifier(settings.GOOGLE_ADS_CONVERSION_ID),
        "enhanced_conversions": settings.GOOGLE_ADS_ENHANCED_CONVERSIONS_ENABLED,
        "consent_mode": settings.GOOGLE_CONSENT_MODE_ENABLED,
        "site_verification": bool(settings.GOOGLE_SITE_VERIFICATION),
        "sitemap_urls": len(public_sitemap_entries()),
        "robots_status": "available",
        "redirect_count": LegacyRedirect.objects.filter(is_active=True).count(),
        "structured_ready": seo_diagnostics()["structured_ready"],
        "blockers": [message for _, message in errors]
        + (
            ["Google integrations are disabled."]
            if not settings.GOOGLE_INTEGRATIONS_ENABLED
            else []
        ),
    }


@staff_member_required
def marketing_diagnostics(request: HttpRequest) -> HttpResponse:
    _allowed(request, "core.view_marketing_diagnostics")
    return render(
        request,
        "admin/core/marketing_diagnostics.html",
        {"title": _("Google and marketing diagnostics"), "status": marketing_status()},
    )


@staff_member_required
def seo_dashboard(request: HttpRequest) -> HttpResponse:
    _allowed(request, "core.view_seo_dashboard")
    return render(
        request,
        "admin/core/seo_dashboard.html",
        {"title": _("Technical SEO dashboard"), "seo": seo_diagnostics()},
    )


@staff_member_required
@require_POST
def validate_marketing_component(request: HttpRequest, component: str) -> HttpResponse:
    _allowed(request, "core.view_marketing_diagnostics")
    checks = {
        "event": lambda: sanitize_analytics_event(
            "view_item",
            {"items": [{"item_id": "diagnostic-property", "quantity": 1}]},
        ),
        "structured_data": lambda: (
            property_structured_data(Property.objects.public().first())
            if Property.objects.public().exists()
            else {}
        ),
        "redirects": lambda: seo_diagnostics()["redirect_loops"],
        "sitemap": lambda: len(public_sitemap_entries()),
    }
    if component not in checks:
        raise PermissionDenied
    result = checks[component]()
    return render(
        request,
        "admin/core/marketing_validation_result.html",
        {
            "title": _("Local validation result"),
            "component": component,
            "valid": result is not None,
        },
    )


@require_POST
def acknowledge_purchase_event(request: HttpRequest) -> HttpResponse:
    """Atomically close the one-time browser bridge after it pushes a purchase."""
    token = request.POST.get("receipt_token", "")
    try:
        payload = signing.loads(
            token,
            salt=PURCHASE_RECEIPT_TOKEN_SALT,
            max_age=60 * 60 * 24 * 7,
        )
    except signing.BadSignature:
        return HttpResponse(status=400)
    if not isinstance(payload, dict) or payload.get("event_name") != "purchase":
        return HttpResponse(status=400)

    MarketingEventReceipt.objects.filter(
        pk=payload.get("receipt_id"),
        event_name="purchase",
        status=MarketingEventReceipt.Status.PREPARED,
    ).update(
        status=MarketingEventReceipt.Status.EMITTED,
        emitted_at=timezone.now(),
    )
    return HttpResponse(status=204)
