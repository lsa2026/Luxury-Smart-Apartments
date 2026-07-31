"""Cached, privacy-safe context shared by public templates."""

import json
from datetime import datetime
from urllib.parse import unquote

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError
from django.http import HttpRequest
from django.utils import timezone, translation

from apps.properties.models import Property

from .models import SiteSetting

GOOGLE_EXCLUDED_PREFIXES = (
    "/admin/",
    "/health/",
    "/integrations/",
)


def _localized_city(row: dict[str, str], language: str) -> str:
    order = {
        "ar": ("city_ar", "city_en", "city"),
        "en": ("city_en", "city", "city_ar"),
    }
    return next(
        (row.get(field, "") for field in order.get(language, order["ar"]) if row.get(field)),
        "",
    )


def _consent_from_cookie(request: HttpRequest) -> dict[str, object] | None:
    raw_value = request.COOKIES.get("lsa_cookie_consent", "")
    if not raw_value:
        return None
    try:
        value = json.loads(unquote(raw_value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("version") != settings.COOKIE_CONSENT_VERSION
        or not isinstance(value.get("analytics"), bool)
        or not isinstance(value.get("marketing"), bool)
    ):
        return None
    return {
        "version": value["version"],
        "analytics": value["analytics"],
        "marketing": value["marketing"],
    }


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
        footer_city_rows = cache.get("site:footer-cities:v2")
        if footer_city_rows is None:
            try:
                footer_city_rows = list(
                    Property.objects.public()
                    .exclude(city="")
                    .values("city", "city_ar", "city_en")
                    .distinct()[:8]
                )
            except DatabaseError:
                footer_city_rows = []
            cache.set("site:footer-cities:v2", footer_city_rows, timeout=300)
    canonical_path = request.path
    canonical_suffix = ""
    page_value = request.GET.get("page", "")
    if request.path == "/properties/" and page_value.isdigit() and int(page_value) > 1:
        canonical_suffix = f"?page={int(page_value)}"
    site_name = site_setting.site_name if site_setting is not None else "Luxury Smart Apartments"
    language = (translation.get_language() or settings.LANGUAGE_CODE).split("-")[0]
    if getattr(request, "_local_public_context", False):
        footer_cities = []
    else:
        footer_cities = [
            city for row in footer_city_rows if (city := _localized_city(row, language))
        ]
    public_marketing_page = not request.path.startswith(GOOGLE_EXCLUDED_PREFIXES) and not getattr(
        request, "_disable_google_integrations", False
    )
    integrations_enabled = settings.GOOGLE_INTEGRATIONS_ENABLED and public_marketing_page
    consent = _consent_from_cookie(request)
    same_as = []
    if site_setting is not None:
        same_as = [
            value
            for value in (
                site_setting.instagram_url,
                site_setting.facebook_url,
                site_setting.x_url,
                site_setting.linkedin_url,
            )
            if value
        ]
    organization_data: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": site_name,
        "url": settings.SITE_CANONICAL_URL,
    }
    if same_as:
        organization_data["sameAs"] = same_as
    if site_setting is not None and (site_setting.contact_email or site_setting.contact_phone):
        organization_data["contactPoint"] = {
            "@type": "ContactPoint",
            "contactType": "customer support",
            **({"email": site_setting.contact_email} if site_setting.contact_email else {}),
            **({"telephone": site_setting.contact_phone} if site_setting.contact_phone else {}),
        }
    pending_event = (
        request.session.pop("analytics_event", None) if hasattr(request, "session") else None
    )
    return {
        "site_setting": site_setting,
        "footer_cities": footer_cities,
        "current_year": datetime.now(tz=timezone.get_current_timezone()).year,
        "canonical_url": (f"{settings.SITE_CANONICAL_URL}{canonical_path}{canonical_suffix}"),
        "hreflang_ar_url": (f"{settings.SITE_CANONICAL_URL}{canonical_path}{canonical_suffix}"),
        "hreflang_en_url": (f"{settings.SITE_CANONICAL_URL}{canonical_path}{canonical_suffix}"),
        "current_language_code": language,
        "csp_nonce": getattr(request, "csp_nonce", ""),
        "organization_data": organization_data,
        "google_config": {
            "google_integrations_enabled": integrations_enabled,
            "gtm_enabled": integrations_enabled and settings.GOOGLE_TAG_MANAGER_ENABLED,
            "gtm_container_id": (
                settings.GOOGLE_TAG_MANAGER_CONTAINER_ID
                if integrations_enabled and settings.GOOGLE_TAG_MANAGER_ENABLED
                else ""
            ),
            "ga4_enabled": integrations_enabled and settings.GOOGLE_ANALYTICS_ENABLED,
            "ga4_measurement_id": (
                settings.GOOGLE_ANALYTICS_MEASUREMENT_ID
                if integrations_enabled and settings.GOOGLE_ANALYTICS_ENABLED
                else ""
            ),
            "google_ads_enabled": integrations_enabled and settings.GOOGLE_ADS_ENABLED,
            "consent_mode_enabled": settings.GOOGLE_CONSENT_MODE_ENABLED,
            "consent_defaults": {
                "analytics_storage": settings.GOOGLE_CONSENT_DEFAULT_ANALYTICS_STORAGE,
                "ad_storage": settings.GOOGLE_CONSENT_DEFAULT_AD_STORAGE,
                "ad_user_data": settings.GOOGLE_CONSENT_DEFAULT_AD_USER_DATA,
                "ad_personalization": (settings.GOOGLE_CONSENT_DEFAULT_AD_PERSONALIZATION),
            },
            "debug_enabled": settings.ANALYTICS_EVENT_DEBUG_ENABLED,
            "ga4_debug_mode": settings.GOOGLE_ANALYTICS_DEBUG_MODE,
            "consent": consent,
        },
        "cookie_consent_enabled": settings.COOKIE_CONSENT_ENABLED and public_marketing_page,
        "cookie_consent_version": settings.COOKIE_CONSENT_VERSION,
        "cookie_consent_max_age": settings.COOKIE_CONSENT_MAX_AGE_DAYS * 86400,
        "cookie_consent_secure": not settings.DEBUG,
        "site_verification_token": (
            settings.GOOGLE_SITE_VERIFICATION
            if integrations_enabled and settings.GOOGLE_SEARCH_CONSOLE_ENABLED
            else ""
        ),
        "pending_analytics_event": (pending_event if isinstance(pending_event, str) else ""),
    }
