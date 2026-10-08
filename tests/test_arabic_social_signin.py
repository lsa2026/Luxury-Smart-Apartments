"""Ensure shipped Arabic catalogs translate the guest social sign-in section."""

import gettext
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client, override_settings

LABELS = {
    "Other secure sign-in options": "خيارات أخرى للدخول الآمن",
    "Or continue with": "أو تابع باستخدام",
    "Continue with Google": "المتابعة باستخدام Google",
    "Continue with Apple": "المتابعة باستخدام Apple",
    "A provider confirms your identity; it never grants administrative access on its own.": (
        "يؤكد مزوّد الدخول هويتك، ولا يمنحك صلاحيات الإدارة بمجرد تسجيل الدخول."
    ),
}


def test_compiled_arabic_catalog_translates_social_sign_in():
    path = Path(settings.BASE_DIR) / "locale/ar/LC_MESSAGES/django.mo"
    with path.open("rb") as source:
        catalog = gettext.GNUTranslations(source)
    for message, expected in LABELS.items():
        assert catalog.gettext(message) == expected


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/login/", "/accounts/login/", "/register/"])
@override_settings(
    GOOGLE_SIGN_IN_ENABLED=True,
    APPLE_SIGN_IN_ENABLED=True,
    SOCIALACCOUNT_PROVIDERS={
        "google": {"APP": {"client_id": "test-only", "secret": "test-only"}},
        "apple": {"APP": {"client_id": "test-only", "secret": "test-only", "key": "test"}},
    },
)
def test_arabic_guest_pages_keep_social_sign_in_localized(path):
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
    response = client.get(path)
    assert response.status_code == 200
    content = response.content.decode()
    assert 'lang="ar"' in content
    assert 'dir="rtl"' in content
    for message, expected in LABELS.items():
        assert expected in content
        assert message not in content
