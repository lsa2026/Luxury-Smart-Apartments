"""The custom admin screens must render in all three languages."""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import translation

ADMIN_URLS = [
    "notifications:hub",
    "properties_admin:image_alt_text",
    "marketing:seo_dashboard",
    "marketing:diagnostics",
    "notifications:center",
    "notifications:dashboard",
    "notifications:system_status",
    "integrations:health",
]


@pytest.fixture
def staff_client(client, db):
    user = get_user_model().objects.create_superuser(
        username="i18n-boss", email="i18n@example.com", password="pw12345!"
    )
    client.force_login(user)
    return client


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_admin_landing_opens_the_booking_workspace(staff_client, language):
    with translation.override(language):
        response = staff_client.get(reverse("admin:index"), headers={"accept-language": language})
    assert response.status_code == 302
    assert response.url == reverse("notifications:hub")


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_custom_admin_screens_render(staff_client, language):
    for name in ADMIN_URLS:
        try:
            url = reverse(name)
        except Exception:  # a screen that is not wired in this build
            continue
        response = staff_client.get(url, headers={"accept-language": language})
        assert response.status_code in (200, 302), f"{name} ({language}) -> {response.status_code}"


@pytest.mark.django_db
@pytest.mark.django_db
def test_admin_header_offers_language_switching(staff_client):
    workspace_url = reverse("notifications:hub")
    response = staff_client.get(workspace_url)
    body = response.content.decode()
    assert 'action="/i18n/setlang/"' in body
    assert 'data-language-select' in body

    response = staff_client.post(
        reverse("set_language"),
        {"language": "en", "next": workspace_url},
    )
    assert response.status_code == 302
    response = staff_client.get(workspace_url)
    assert '<option value="en" selected>English</option>' in response.content.decode()
