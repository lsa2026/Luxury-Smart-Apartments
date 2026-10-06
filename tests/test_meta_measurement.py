"""Public-only Meta integration boundaries; synthetic data, no provider calls."""

import pytest
from django.test import Client, override_settings

from tests.test_booking_views_admin import owned_client_quote

pytestmark = pytest.mark.django_db

GOOGLE = {
    "GOOGLE_INTEGRATIONS_ENABLED": True,
    "GOOGLE_TAG_MANAGER_ENABLED": True,
    "GOOGLE_TAG_MANAGER_CONTAINER_ID": "GTM-ABCD1",
}


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_public_pages_offer_meta_bridge_without_inline_pixel_or_relaxing_csp(language):
    with override_settings(**GOOGLE):
        response = Client().get(f"/{language}/")
    content = response.content.decode()
    assert response.status_code == 200
    assert "js/meta-measurement.js" in content
    assert 'data-meta-measurement-page-allowed="true"' in content
    assert "Facebook" in content or "فيسبوك" in content
    csp = response["Content-Security-Policy"]
    assert "https://connect.facebook.net" in csp
    assert "https://www.facebook.com" in csp
    script_csp = next(part for part in csp.split(";") if part.strip().startswith("script-src "))
    assert "'unsafe-inline'" not in script_csp and "'unsafe-eval'" not in script_csp
    assert "https://connect.facebook.net" not in content
    assert "js/analytics.js?v=18" in content


def test_private_checkout_preserves_google_but_cannot_load_meta():
    client, _quote, reference = owned_client_quote()
    with override_settings(**GOOGLE):
        response = client.get(f"/reservations/quotes/{reference}/")
    content = response.content.decode()
    assert response.status_code == 200
    assert "js/meta-measurement.js" not in content
    assert 'data-meta-measurement-page-allowed="false"' in content
    assert "connect.facebook.net" not in response["Content-Security-Policy"]
    assert "js/analytics.js?v=18" in content
    assert 'data-analytics-event="begin_checkout"' in content


@pytest.mark.parametrize("url", ["/synthetic-missing-page/", "/accounts/login/", "/admin/login/"])
def test_nonpublic_pages_cannot_load_meta_even_with_google_enabled(url):
    with override_settings(**GOOGLE):
        response = Client().get(url)
    assert "js/meta-measurement.js" not in response.content.decode()
    assert "connect.facebook.net" not in response.get("Content-Security-Policy", "")


@pytest.mark.parametrize(
    "overrides",
    [
        {"GOOGLE_INTEGRATIONS_ENABLED": False},
        {**GOOGLE, "GOOGLE_TAG_MANAGER_ENABLED": False},
    ],
)
def test_disabled_integration_does_not_offer_bridge_or_meta_csp(overrides):
    with override_settings(**overrides):
        response = Client().get("/en/")
    assert "js/meta-measurement.js" not in response.content.decode()
    assert "connect.facebook.net" not in response["Content-Security-Policy"]


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("page", ["privacy", "cookies"])
def test_meta_measurement_disclosure_is_present_in_each_policy(language, page):
    content = Client().get(f"/{language}/legal/{page}/").content.decode()
    assert "Meta Pixel" in content
    assert "facebook.com/privacy/policy/" in content
    assert "SHA-256" in content  # Existing Google disclosure remains.
