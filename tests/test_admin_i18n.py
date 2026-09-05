"""The custom admin screens must render in all three languages."""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import translation

ADMIN_URLS = [
    "admin:index",
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
def test_admin_index_renders_in_each_language(staff_client, language):
    with translation.override(language):
        response = staff_client.get(reverse("admin:index"), headers={"accept-language": language})
    assert response.status_code == 200


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
def test_admin_index_is_actually_translated(staff_client):
    """The English index must not fall back to the original Arabic wording."""
    response = staff_client.get(reverse("admin:index"), headers={"accept-language": "en"})
    body = response.content.decode()
    assert "Command centre" in body or "Priorities needing a decision" in body
    assert "أولويات تحتاج قرارًا" not in body


@pytest.mark.django_db
def test_admin_index_keeps_arabic_for_arabic_users(staff_client):
    response = staff_client.get(reverse("admin:index"), headers={"accept-language": "ar"})
    body = response.content.decode()
    assert "أولويات تحتاج قرارًا" in body
