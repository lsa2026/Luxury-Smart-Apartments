"""Ensure shipped Arabic catalogs translate the guest social sign-in section."""

import gettext
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
    response = client.get(path, {"next": "/my-bookings/"})
    assert response.status_code == 200
    content = response.content.decode()
    assert 'lang="ar"' in content
    assert 'dir="rtl"' in content
    for message, expected in LABELS.items():
        assert expected in content
        assert message not in content
    assert 'class="auth-social__google"' not in content
    assert 'class="auth-social__apple"' not in content
    assert 'src="/static/images/auth/google.png"' in content
    assert 'src="/static/images/auth/apple.svg"' in content
    assert content.count('class="auth-social__logo"') == 2
    actions = [
        urlsplit(unescape(action)) for action in re.findall(r'<form[^>]*action="([^"]+)"', content)
    ]
    for provider in ("google", "apple"):
        action = next(action for action in actions if action.path == f"/accounts/{provider}/login/")
        assert parse_qs(action.query) == {"process": ["login"], "next": ["/my-bookings/"]}


def test_provider_marks_are_local_assets_not_network_dependencies():
    assets = Path(settings.BASE_DIR) / "static/images/auth"
    assert (assets / "google.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    apple = (assets / "apple.svg").read_text(encoding="utf-8")
    assert '<path fill="#000"' in apple
    assert "<script" not in apple
