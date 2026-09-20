"""Regression coverage for the September 18 Blueprint owner-login lockout."""

import re
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = {"google": {"APP": {"client_id": "test-client-id", "secret": "test-secret"}}}
READY = {
    "GOOGLE_SIGN_IN_ENABLED": True,
    "GOOGLE_OAUTH_CLIENT_ID": "test-client-id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "test-secret",
    "SOCIALACCOUNT_LOGIN_ON_GET": True,
    "OPERATIONS_OWNER_ENFORCEMENT_ENABLED": True,
    "SOCIALACCOUNT_PROVIDERS": PROVIDERS,
}


def test_blueprint_preserves_owner_login_and_requires_preflight():
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    entry = re.search(
        r"^      - key: GOOGLE_SIGN_IN_ENABLED\n((?:        [^\n]*\n)+)",
        blueprint,
        re.MULTILINE,
    )
    assert entry
    assert "sync: false" in entry[1]
    assert "value:" not in entry[1]
    assert "buildCommand: ./build.sh --require-owner-login" in blueprint
    build = (ROOT / "build.sh").read_text(encoding="utf-8")
    assert build.index("python manage.py check_owner_login") < build.index(
        "python manage.py migrate"
    )


@override_settings(**READY)
def test_ready_owner_login_passes_without_database():
    output = StringIO()
    call_command("check_owner_login", stdout=output)
    assert "configuration is ready" in output.getvalue()
    assert "test-secret" not in output.getvalue()


@pytest.mark.parametrize("key", list(READY))
def test_preflight_rejects_a_disabled_or_incomplete_owner_entrance(key):
    config = {**READY, key: {} if key == "SOCIALACCOUNT_PROVIDERS" else False}
    with override_settings(**config), pytest.raises(CommandError) as error:
        call_command("check_owner_login")
    assert key in str(error.value)
    assert "test-secret" not in str(error.value)


@override_settings(**READY)
def test_owner_login_link_reaches_google_with_hub_return_path(db):
    client = Client()
    page = client.get("/admin/login/?next=/admin/operations/hub/")
    assert page.status_code == 200
    assert "غير مهيأ" not in page.content.decode()
    assert "/accounts/google/login/" in page.content.decode()
    response = client.get("/accounts/google/login/?process=login&next=/admin/operations/hub/")
    assert response.status_code == 302
    assert response.url.startswith("https://accounts.google.com/")
    assert "state=" in response.url
