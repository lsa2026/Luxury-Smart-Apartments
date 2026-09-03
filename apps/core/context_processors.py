"""Cached, privacy-safe context shared by public templates."""

import json
from datetime import datetime
from urllib.parse import quote, unquote

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError
from django.http import HttpRequest
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from apps.properties.cities import supported_city_labels

from .branding import BRAND_NAME
from .models import SiteSetting

GOOGLE_EXCLUDED_PREFIXES = (
    "/admin/",
    "/health/",
    "/integrations/",
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


def _whatsapp_digits(raw: str) -> str:
    """Normalise a dialled number to the digits-only form wa.me expects."""
    digits = "".join(character for character in raw if character.isdigit())
    if not digits:
        return ""
    country_code = settings.WHATSAPP_DEFAULT_COUNTRY_CODE
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = f"{country_code}{digits.lstrip('0')}"
    elif not digits.startswith(country_code) and len(digits) <= 9:
        digits = f"{country_code}{digits}"
    return digits


def _whatsapp_contact(site_setting: SiteSetting | None) -> dict[str, str]:
    """Floating WhatsApp button target, admin-managed with a settings fallback."""
    raw_number = ""
    configured_url = ""
    if site_setting is not None:
        raw_number = site_setting.whatsapp_display_number or site_setting.contact_phone or ""
        configured_url = site_setting.whatsapp_url or ""
    digits = _whatsapp_digits(raw_number or settings.WHATSAPP_CONTACT_NUMBER)
    if not configured_url and not digits:
        return {"url": "", "display": ""}
    greeting = _("Hello, I would like to ask about a stay with Luxury Smart Apartments.")
    url = configured_url or f"https://wa.me/{digits}?text={quote(greeting)}"
    return {"url": url, "display": f"+{digits}" if digits else ""}


def site_context(request: HttpRequest) -> dict[str, object]:
    if getattr(request, "_local_public_context", False):
        site_setting = None
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
    canonical_path = request.path
    canonical_suffix = ""
    page_value = request.GET.get("page", "")
    if request.path == "/properties/" and page_value.isdigit() and int(page_value) > 1:
        canonical_suffix = f"?page={int(page_value)}"
    site_name = BRAND_NAME
    language = (translation.get_language() or settings.LANGUAGE_CODE).split("-")[0]
    footer_cities = supported_city_labels(language)
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
        "hreflang_fr_url": (f"{settings.SITE_CANONICAL_URL}{canonical_path}{canonical_suffix}"),
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
        "payment_sandbox_enabled": settings.PAYMENT_SANDBOX_ENABLED,
        "whatsapp_contact": _whatsapp_contact(site_setting),
    }
