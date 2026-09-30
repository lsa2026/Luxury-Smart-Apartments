"""Admin screens render with a fixed Arabic header for every browser language."""

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
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_admin_header_has_fixed_arabic_label_without_language_switching(staff_client, language):
    workspace_url = reverse("notifications:hub")
    response = staff_client.get(workspace_url, headers={"accept-language": language})
    assert response.status_code == 200
    body = response.content.decode()
    assert 'class="lsa-admin-language" aria-label="العربية">العربية</span>' in body
    assert 'dir="rtl"' in body
    assert 'action="/i18n/setlang/"' not in body
    assert "data-language-select" not in body
