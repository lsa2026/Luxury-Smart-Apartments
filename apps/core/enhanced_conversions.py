"""Optional Ads-only purchase identity, outside the public analytics payload."""

import hashlib
import json
from urllib.parse import unquote

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpRequest


def consented_purchase_email_hash(request: HttpRequest, email: str) -> str:
    """Fail closed; callers must additionally require a verified purchase.

    The current request's independent opt-in is required, not merely an Ads
    cookie or a past booking. No identity is persisted in tracking receipts.
    """
    if not (
        settings.GOOGLE_INTEGRATIONS_ENABLED
        and settings.GOOGLE_TAG_MANAGER_ENABLED
        and settings.GOOGLE_ADS_ENABLED
        and settings.COOKIE_CONSENT_ENABLED
    ):
        return ""
    try:
        choice = json.loads(unquote(request.COOKIES.get("lsa_cookie_consent", "")))
    except (ValueError, TypeError):
        return ""
    if not isinstance(choice, dict) or not (
        choice.get("version") == settings.COOKIE_CONSENT_VERSION
        and choice.get("analytics") is True
        and choice.get("marketing") is True
        and choice.get("userProvidedData") is True
        and choice.get("userProvidedDataVersion") == 1
    ):
        return ""
    normalized = str(email or "").strip().lower()
    try:
        validate_email(normalized)
    except ValidationError:
        return ""
    local, domain = normalized.rsplit("@", 1)
    if domain in {"gmail.com", "googlemail.com"}:
        normalized = f"{local.replace('.', '')}@{domain}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
