"""Keep OAuth POST redirects usable without relaxing unrelated page CSP."""

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from apps.core.middleware import SecurityHeadersMiddleware


def policy_for(path):
    request = RequestFactory().get(path)
    response = SecurityHeadersMiddleware(lambda request: HttpResponse())(request)
    return {
        directive.split()[0]: directive.split()[1:]
        for directive in response["Content-Security-Policy"].split(";")
        if directive.strip()
    }


@pytest.mark.parametrize(
    "path", ["/login/", "/register/", "/accounts/login/", "/accounts/signup/"]
)
@override_settings(APPLE_SIGN_IN_ENABLED=True, GOOGLE_SIGN_IN_ENABLED=True)
def test_guest_sign_in_allows_only_exact_enabled_provider_form_redirects(path):
    policy = policy_for(path)
    assert policy["form-action"] == [
        "'self'",
        "https://appleid.apple.com",
        "https://accounts.google.com",
    ]
    assert policy["frame-ancestors"] == ["'none'"]
    assert policy["object-src"] == ["'none'"]
    for name in ("script-src", "connect-src", "frame-src"):
        assert "https://appleid.apple.com" not in policy[name]
        assert "https://accounts.google.com" not in policy[name]


@pytest.mark.parametrize(
    "path, origin",
    [
        ("/accounts/apple/login/", "https://appleid.apple.com"),
        ("/accounts/google/login/", "https://accounts.google.com"),
    ],
)
@override_settings(APPLE_SIGN_IN_ENABLED=True, GOOGLE_SIGN_IN_ENABLED=True)
def test_oauth_endpoint_allows_its_own_provider_only(path, origin):
    assert policy_for(path)["form-action"] == ["'self'", origin]


@pytest.mark.parametrize(
    "path",
    [
        "/ar/",
        "/en/properties/",
        "/my-bookings/",
        "/reservations/manage/",
        "/admin/login/",
        "/admin/operations/hub/",
        "/accounts/apple/login/callback/",
        "/accounts/apple/login/callback/finish/",
        "/accounts/apple/login/unexpected/",
        "/login/unexpected/",
    ],
)
@override_settings(APPLE_SIGN_IN_ENABLED=True, GOOGLE_SIGN_IN_ENABLED=True)
def test_unrelated_and_callback_pages_keep_same_origin_form_policy(path):
    assert policy_for(path)["form-action"] == ["'self'"]


@pytest.mark.parametrize("path", ["/login/", "/accounts/apple/login/", "/accounts/google/login/"])
@override_settings(APPLE_SIGN_IN_ENABLED=False, GOOGLE_SIGN_IN_ENABLED=False)
def test_disabled_social_providers_do_not_expand_form_policy(path):
    assert policy_for(path)["form-action"] == ["'self'"]


@override_settings(APPLE_SIGN_IN_ENABLED=True, GOOGLE_SIGN_IN_ENABLED=False)
def test_apple_only_guest_login_does_not_allow_google():
    assert policy_for("/login/")["form-action"] == ["'self'", "https://appleid.apple.com"]
