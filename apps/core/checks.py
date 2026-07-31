"""Configuration checks for disabled-by-default Google integrations."""

import re

from django.conf import settings
from django.core.checks import Error, Tags, register

GTM_ID_PATTERN = re.compile(r"^GTM-[A-Z0-9]{4,}$")
GA4_ID_PATTERN = re.compile(r"^G-[A-Z0-9]{4,}$")
GOOGLE_ADS_ID_PATTERN = re.compile(r"^AW-\d{6,}$")
CONVERSION_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,}$")


def validate_google_configuration() -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    if not settings.GOOGLE_INTEGRATIONS_ENABLED:
        return errors
    if settings.GOOGLE_TAG_MANAGER_ENABLED and not GTM_ID_PATTERN.fullmatch(
        settings.GOOGLE_TAG_MANAGER_CONTAINER_ID
    ):
        errors.append(("core.E101", "A valid GTM container ID is required."))
    if settings.GOOGLE_ANALYTICS_ENABLED and not GA4_ID_PATTERN.fullmatch(
        settings.GOOGLE_ANALYTICS_MEASUREMENT_ID
    ):
        errors.append(("core.E102", "A valid GA4 measurement ID is required."))
    if settings.GOOGLE_ADS_ENABLED:
        if not GOOGLE_ADS_ID_PATTERN.fullmatch(settings.GOOGLE_ADS_CONVERSION_ID):
            errors.append(("core.E103", "A valid Google Ads conversion ID is required."))
        for label_name in (
            "GOOGLE_ADS_BOOKING_CONVERSION_LABEL",
            "GOOGLE_ADS_CONTACT_CONVERSION_LABEL",
        ):
            if not CONVERSION_LABEL_PATTERN.fullmatch(getattr(settings, label_name)):
                errors.append(("core.E104", f"A valid {label_name} is required."))
    if settings.GOOGLE_ADS_ENHANCED_CONVERSIONS_ENABLED:
        errors.append(("core.E105", "Enhanced Conversions are not supported in this phase."))
    consent_values = {
        settings.GOOGLE_CONSENT_DEFAULT_ANALYTICS_STORAGE,
        settings.GOOGLE_CONSENT_DEFAULT_AD_STORAGE,
        settings.GOOGLE_CONSENT_DEFAULT_AD_USER_DATA,
        settings.GOOGLE_CONSENT_DEFAULT_AD_PERSONALIZATION,
    }
    if not consent_values.issubset({"denied", "granted"}):
        errors.append(("core.E106", "Google consent defaults must be denied or granted."))
    return errors


@register(Tags.security)
def google_configuration_check(
    app_configs: object = None,
    **kwargs: object,
) -> list[Error]:
    del app_configs, kwargs
    return [Error(message, id=error_id) for error_id, message in validate_google_configuration()]
