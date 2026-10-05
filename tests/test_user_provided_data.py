"""Synthetic UPD UI and privacy regression checks; no Google/provider calls."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client, override_settings

from tests.test_booking_views_admin import owned_client_quote

pytestmark = pytest.mark.django_db

GOOGLE = {
    "GOOGLE_INTEGRATIONS_ENABLED": True,
    "GOOGLE_TAG_MANAGER_ENABLED": True,
    "GOOGLE_TAG_MANAGER_CONTAINER_ID": "GTM-ABCD1",
    "GOOGLE_ANALYTICS_ENABLED": True,
    "GOOGLE_ANALYTICS_MEASUREMENT_ID": "G-ABCD1",
}


@pytest.mark.parametrize(
    ("language", "label"),
    [
        ("ar", "مطابقة البريد لتحسين القياس — اختياري"),
        ("en", "Email matching for measurement — optional"),
        ("fr", "Correspondance de l’e-mail pour la mesure — facultatif"),
    ],
)
def test_independent_opt_in_is_translated_unchecked_and_not_required(language, label):
    with override_settings(**GOOGLE):
        content = Client().get(f"/{language}/").content.decode()
    assert label in content
    controls = re.findall(r"<input[^>]*data-consent-upd[^>]*>", content)
    assert len(controls) == 1
    assert "checked" not in controls[0] and "required" not in controls[0]
    assert 'name="' not in controls[0]
    assert "js/user-provided-data.js" not in content


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_checkout_scope_has_optional_controls_without_changing_booking_fields(language):
    client, _quote, reference = owned_client_quote()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    with override_settings(**GOOGLE):
        response = client.get(f"/reservations/quotes/{reference}/")
    content = response.content.decode()
    assert response.status_code == 200
    assert "data-upd-guest-form" in content
    assert "js/user-provided-data.js" in content
    assert content.count("data-consent-upd") == 2  # Same preference, form and dialog.
    assert 'name="guest_email"' in content
    assert 'name="terms_accepted"' in content and 'name="house_rules_accepted"' in content
    assert 'data-analytics-event="begin_checkout"' in content
    assert "js/analytics.js" in content and "?v=17" in content
    assert 'dir="rtl"' in content if language == "ar" else 'dir="ltr"' in content


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("page", ["privacy", "cookies"])
def test_privacy_and_cookie_policies_describe_hashing_and_withdrawal(language, page):
    content = Client().get(f"/{language}/legal/{page}/").content.decode()
    assert "SHA-256" in content
    assert "Google Ads" in content
    assert "policies.google.com/privacy" in content


def test_disabled_google_or_missing_ga4_does_not_offer_identity_collection():
    client, _quote, reference = owned_client_quote()
    for overrides in (
        {"GOOGLE_INTEGRATIONS_ENABLED": False},
        {**GOOGLE, "GOOGLE_ANALYTICS_ENABLED": False},
    ):
        with override_settings(**overrides):
            content = client.get(f"/reservations/quotes/{reference}/").content.decode()
        assert "data-consent-upd" not in content
        assert "js/user-provided-data.js" not in content


def test_upd_script_cannot_modify_purchase_tracking_or_store_identity():
    code = Path("static/js/user-provided-data.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in code.splitlines() if not line.strip().startswith("//"))
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "transaction_id",
        "ecommerce",
        "eventCallback",
        "receiptToken",
        "preventDefault(",
    ):
        assert forbidden not in code
    assert 'event: "lsa_user_data_provided"' in code
    assert "sha256_email_address" in code
