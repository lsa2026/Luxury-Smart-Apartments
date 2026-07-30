"""Cached, privacy-safe context shared by public templates."""

from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError
from django.http import HttpRequest
from django.utils import timezone

from apps.properties.models import Property

from .models import SiteSetting


def site_context(request: HttpRequest) -> dict[str, object]:
    if getattr(request, "_local_public_context", False):
        site_setting = None
        footer_cities: list[str] = []
    else:
        site_setting = cache.get("site:settings")
        if site_setting is None:
            try:
                site_setting = SiteSetting.objects.first()
            except DatabaseError:
                site_setting = None
            cache.set("site:settings", site_setting or False, timeout=300)
        if site_setting is False:
            site_setting = None
        footer_cities = cache.get("site:footer-cities")
        if footer_cities is None:
            try:
                footer_cities = list(
                    Property.objects.public()
                    .exclude(city="")
                    .values_list("city", flat=True)
                    .distinct()[:8]
                )
            except DatabaseError:
                footer_cities = []
            cache.set("site:footer-cities", footer_cities, timeout=300)
    canonical_path = request.path
    site_name = site_setting.site_name if site_setting is not None else "Luxury Smart Apartments"
    return {
        "site_setting": site_setting,
        "footer_cities": footer_cities,
        "current_year": datetime.now(tz=timezone.get_current_timezone()).year,
        "canonical_url": f"{settings.SITE_CANONICAL_URL}{canonical_path}",
        "csp_nonce": getattr(request, "csp_nonce", ""),
        "organization_data": {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": site_name,
            "url": settings.SITE_CANONICAL_URL,
        },
    }
