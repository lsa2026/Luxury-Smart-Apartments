"""Clarity CSP scope only; no provider calls or payment replays."""

import pytest
from django.test import Client, override_settings

pytestmark = pytest.mark.django_db

GOOGLE = {
    "GOOGLE_INTEGRATIONS_ENABLED": True,
    "GOOGLE_TAG_MANAGER_ENABLED": True,
    "GOOGLE_TAG_MANAGER_CONTAINER_ID": "GTM-ABCD1",
}


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("page", ["", "about/", "faq/", "legal/privacy/", "legal/cookies/"])
def test_clean_public_pages_allow_only_scoped_clarity_without_inline_permission(language, page):
    with override_settings(**GOOGLE):
        response = Client().get(f"/{language}/{page}")
    assert response.status_code == 200
    csp = response["Content-Security-Policy"]
    directives = {p.strip().split()[0]: p for p in csp.split(";") if p.strip()}
    assert "https://www.clarity.ms" in directives["script-src"]
    assert "https://scripts.clarity.ms" in directives["script-src"]
    assert "https://*.clarity.ms" in directives["connect-src"]
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-eval'" not in directives["script-src"]
    assert "clarity" not in directives["default-src"]
    assert "clarity" not in directives["frame-src"]
    assert "js/clarity-public.js" not in response.content.decode()  # GTM activates only.
    assert "data-consent-clarity" in response.content.decode()


@pytest.mark.parametrize(
    "url",
    [
        "/ar/contact/",
        "/accounts/login/",
        "/admin/login/",
        "/my-bookings/",
        "/payments/hyperpay/result/synthetic/",
        "/reservations/synthetic/",
        "/ar/?return_token=synthetic",
        "/ar/?utm_source=synthetic",
        "/synthetic-404/",
    ],
)
def test_excluded_routes_and_queries_never_allow_clarity_destinations(url):
    with override_settings(**GOOGLE):
        response = Client().get(url)
    assert "clarity.ms" not in response.get("Content-Security-Policy", "")
    assert "c.bing.com" not in response.get("Content-Security-Policy", "")


@pytest.mark.parametrize(
    "overrides",
    [
        {**GOOGLE, "GOOGLE_INTEGRATIONS_ENABLED": False},
        {**GOOGLE, "GOOGLE_TAG_MANAGER_ENABLED": False},
    ],
)
def test_google_master_and_gtm_switches_still_block_clarity(overrides):
    with override_settings(**overrides):
        response = Client().get("/ar/")
    assert "clarity.ms" not in response["Content-Security-Policy"]


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("page", ["privacy", "cookies"])
def test_disclosure_is_in_all_policy_languages(language, page):
    content = Client().get(f"/{language}/legal/{page}/").content.decode()
    assert "Microsoft Clarity" in content
    assert "privacy.microsoft.com/privacystatement" in content
    assert "SHA-256" in content  # Existing Google disclosure remains.
