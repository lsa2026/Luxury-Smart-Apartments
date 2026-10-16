"""Synthetic Google identities only: grants do not create real users or send mail."""

import re
from html import unescape
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin
from allauth.socialaccount.providers.google.provider import GoogleProvider
from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from apps.accounts.access import MANAGER_SESSION_KEY, is_operations_owner
from apps.accounts.models import OperationsManager
from apps.accounts.social_adapters import LuxurySocialAccountAdapter
from apps.notifications.models import AuditLog

pytestmark = pytest.mark.django_db
OWNER = "saeed@luxurysmartapartments.com"
MANAGER = "synthetic.lsa.manager@gmail.com"


@pytest.fixture(autouse=True)
def strict_access():
    with override_settings(OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True, OPERATIONS_OWNER_EMAIL=OWNER):
        yield


@pytest.fixture
def owner():
    return get_user_model().objects.create_superuser(
        username=OWNER, email=OWNER, password="test-only-password"
    )


def login_manager(email=MANAGER, *, google=True, verified=True):
    """Exercise the actual adapter and auth signal, not a manually forged marker."""
    user = get_user_model().objects.create_user(
        username=email, email=email, password="test-only-password"
    )
    request = RequestFactory().get("/accounts/google/login/callback/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    request.user = AnonymousUser()
    if google:
        SocialApp.objects.get_or_create(
            provider="google",
            defaults={"name": "Test", "client_id": "test-id", "secret": "test-secret"},
        )
        social = SocialLogin(
            user=user,
            account=SocialAccount(
                provider="google",
                uid="synthetic-google-id",
                extra_data={"email": email, "email_verified": verified},
            ),
            email_addresses=[EmailAddress(email=email, verified=verified, primary=True)],
        )
        LuxurySocialAccountAdapter().pre_social_login(request, social)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.save()
    client = Client()
    client.cookies[settings.SESSION_COOKIE_NAME] = request.session.session_key
    return user, client


def test_owner_adds_and_revokes_without_deleting_guest_account(owner):
    client = Client()
    client.force_login(owner)
    path = reverse("accounts:operations_users")
    html = client.get(path).content.decode()
    assert "إدارة المستخدمين" in html
    assert html.count("<h1>") == 1
    count = get_user_model().objects.count()
    response = client.post(
        path, {"email": MANAGER.upper(), "display_name": "مدير تجربة", "confirm_access": "on"}
    )
    assert response.status_code == 302
    grant = OperationsManager.objects.get()
    assert grant.email == MANAGER and grant.created_by == owner
    assert get_user_model().objects.count() == count
    user, manager = login_manager()
    assert not user.is_staff and not user.is_superuser and not is_operations_owner(user)
    assert manager.get(reverse("notifications:hub")).status_code == 200
    revoke = reverse("accounts:operations_user_revoke", args=[grant.pk])
    assert client.get(revoke).status_code == 405
    client.post(revoke)
    grant.refresh_from_db()
    assert grant.is_enabled
    client.post(revoke, {"confirm_revoke": "on"})
    assert manager.get(reverse("notifications:hub")).status_code == 403
    assert get_user_model().objects.filter(pk=user.pk, is_active=True).exists()
    assert AuditLog.objects.filter(action="operations_manager.granted").count() == 1
    assert AuditLog.objects.filter(action="operations_manager.revoked").count() == 1
    client.post(path, {"email": MANAGER, "confirm_access": "on"})
    assert manager.get(reverse("notifications:hub")).status_code == 403  # old session cannot revive
    assert OperationsManager.objects.count() == 1


@pytest.mark.parametrize(
    "email,confirmed", [("wrong@example.invalid", True), (OWNER, True), (MANAGER, False)]
)
def test_invalid_or_unconfirmed_grants_cannot_be_created(owner, email, confirmed):
    client = Client()
    client.force_login(owner)
    data = {"email": email, "confirm_access": "on" if confirmed else ""}
    assert client.post(reverse("accounts:operations_users"), data).status_code == 200
    assert not OperationsManager.objects.exists()


@pytest.mark.parametrize(
    "google,verified,allowed", [(True, True, True), (True, False, False), (False, False, False)]
)
def test_only_verified_google_session_can_use_added_email(google, verified, allowed):
    OperationsManager.objects.create(email=MANAGER)
    _, client = login_manager(google=google, verified=verified)
    assert client.get(reverse("notifications:hub")).status_code == (200 if allowed else 403)


def test_google_user_not_on_allowlist_is_denied():
    _, client = login_manager()
    assert client.get(reverse("notifications:hub")).status_code == 403


@pytest.mark.parametrize("existing", [False, True])
@override_settings(GOOGLE_SIGN_IN_ENABLED=True, SOCIALACCOUNT_LOGIN_ON_GET=True)
def test_complete_google_callback_grants_access_to_new_and_existing_accounts(existing):
    OperationsManager.objects.create(email=MANAGER)
    if existing:
        get_user_model().objects.create_user(username=MANAGER, email=MANAGER)
    app = SocialApp.objects.create(
        provider="google", name="Test", client_id="test-id", secret="test-secret"
    )
    client = Client()
    start = client.get("/accounts/google/login/?process=login&next=/admin/operations/hub/")
    state = parse_qs(urlsplit(start.url).query)["state"][0]
    social = SocialLogin(
        provider=GoogleProvider(request=None, app=app),
        user=get_user_model()(email=MANAGER),
        account=SocialAccount(
            provider="google",
            uid="synthetic-callback-id",
            extra_data={"email": MANAGER, "email_verified": True},
        ),
        email_addresses=[EmailAddress(email=MANAGER, verified=True, primary=True)],
    )
    with (
        patch(
            "allauth.socialaccount.providers.oauth2.client.OAuth2Client.get_access_token",
            return_value={"access_token": "synthetic-only", "token_type": "Bearer"},
        ),
        patch(
            "allauth.socialaccount.providers.google.views.GoogleOAuth2Adapter.complete_login",
            return_value=social,
        ),
    ):
        response = client.get(
            "/accounts/google/login/callback/", {"code": "synthetic-code", "state": state}
        )
    assert response.status_code == 302
    assert response.url == reverse("notifications:hub")
    assert MANAGER_SESSION_KEY in client.session
    assert client.get(reverse("notifications:hub")).status_code == 200
    user = get_user_model().objects.get(email=MANAGER)
    assert not user.is_staff and not user.is_superuser


def test_revoked_manager_cannot_send_or_modify_via_direct_post():
    grant = OperationsManager.objects.create(email=MANAGER)
    _, client = login_manager()
    grant.is_enabled = False
    grant.access_version += 1
    grant.save()
    synthetic_id = "00000000-0000-4000-8000-000000000001"
    with (
        patch("apps.notifications.invoice_views.queue_invoice_delivery") as send,
        patch("apps.notifications.invoice_views.HostawayClient") as api,
    ):
        for name, kwargs in (
            ("notifications:invoice_send", {"invoice_id": synthetic_id}),
            ("notifications:booking_detail", {"reservation_id": synthetic_id}),
            ("notifications:manual_booking_create", {}),
        ):
            assert (
                client.post(reverse(name, kwargs=kwargs), {"channel": "whatsapp"}).status_code
                == 403
            )
        send.assert_not_called()
        api.assert_not_called()


@override_settings(LOYALTY_ENABLED=True, LOYALTY_UI_ENABLED=True)
def test_manager_can_use_booking_invoice_screens_but_not_owner_or_system_tools():
    grant = OperationsManager.objects.create(email=MANAGER)
    user, client = login_manager()
    with patch("apps.notifications.invoice_views.HostawayClient") as api:
        for name in (
            "notifications:hub",
            "notifications:booking_list",
            "notifications:invoice_list",
            "notifications:manual_booking_create",
        ):
            response = client.get(reverse(name))
            assert response.status_code == 200, name
            assert "no-store" in response["Cache-Control"]
        api.assert_not_called()
    html = client.get(reverse("notifications:hub")).content.decode()
    assert html.count('class="lsa-admin-home__card"') == 2
    assert reverse("accounts:operations_users") not in html
    assert reverse("loyalty:admin") not in html
    assert "إدارة برنامج الولاء" not in html
    for path in (
        reverse("accounts:operations_users"),
        reverse("accounts:operations_user_revoke", args=[grant.pk]),
        "/admin/auth/user/",
        "/admin/core/sitesetting/",
        reverse("notifications:system_status"),
        reverse("notifications:dashboard"),
        "/admin/loyalty/",
    ):
        assert client.get(path).status_code == 403, path
        assert (
            client.post(path, {"email": "attack@gmail.com", "confirm_access": "on"}).status_code
            == 403
        )
    assert OperationsManager.objects.count() == 1
    user.refresh_from_db()
    assert not user.is_staff and not user.is_superuser


def test_non_google_relogin_clears_existing_manager_proof():
    OperationsManager.objects.create(email=MANAGER)
    user, client = login_manager()
    session = client.session
    assert MANAGER_SESSION_KEY in session
    request = RequestFactory().post("/login/")
    request.session = session
    request.user = user
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.save()
    assert MANAGER_SESSION_KEY not in client.session
    assert client.get(reverse("notifications:hub")).status_code == 403


def test_owner_access_survives_manager_changes_and_csrf_is_required(owner):
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    assert (
        client.post(
            reverse("accounts:operations_users"), {"email": MANAGER, "confirm_access": "on"}
        ).status_code
        == 403
    )
    assert client.get(reverse("notifications:hub")).status_code == 200
    assert not OperationsManager.objects.exists()


@override_settings(
    GOOGLE_SIGN_IN_ENABLED=True,
    SOCIALACCOUNT_LOGIN_ON_GET=True,
    SOCIALACCOUNT_PROVIDERS={"google": {"OAUTH_PKCE_ENABLED": True}},
)
def test_admin_google_button_forces_account_choice_without_changing_guest_flow():
    SocialApp.objects.create(
        provider="google", name="Test", client_id="test-id", secret="test-secret"
    )
    client = Client()
    html = client.get("/admin/login/?next=/admin/operations/hub/").content.decode()
    link = unescape(re.search(r'href="([^"]+)" class="lsa-admin-login__google"', html)[1])
    response = client.get(link)
    params = parse_qs(urlsplit(response.url).query)
    assert urlsplit(response.url).hostname == "accounts.google.com"
    assert params["prompt"] == ["select_account"]
    assert "state" in params and "code_challenge" in params
    guest = client.get("/accounts/google/login/?process=login&next=/my-bookings/")
    assert "prompt" not in parse_qs(urlsplit(guest.url).query)
