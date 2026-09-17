"""Phase 6.0B: owner identity and administration isolation."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.management import call_command
from django.test import Client, override_settings
from django.test.client import RequestFactory
from django.urls import reverse

from apps.accounts.access import is_operations_owner
from apps.accounts.forms import CustomerRegistrationForm
from apps.accounts.social_adapters import LuxurySocialAccountAdapter

OWNER_EMAIL = "saeed@luxurysmartapartments.com"


def _admin(email: str, username: str):
    return get_user_model().objects.create_superuser(
        username=username,
        email=email,
        password="pw12345!",
    )


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_only_the_designated_business_email_can_open_operations_hub(db):
    owner = _admin(OWNER_EMAIL, "operations-owner")
    other_admin = _admin("other-admin@example.invalid", "other-admin")

    owner_client = Client()
    owner_client.force_login(owner)
    assert owner_client.get(reverse("notifications:hub")).status_code == 200

    other_client = Client()
    other_client.force_login(other_admin)
    assert other_client.get(reverse("notifications:hub")).status_code == 403


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_only_the_designated_business_email_can_open_the_django_admin(db):
    owner = _admin(OWNER_EMAIL, "operations-owner")
    other_admin = _admin("other-admin@example.invalid", "other-admin")

    owner_client = Client()
    owner_client.force_login(owner)
    assert owner_client.get(reverse("admin:index")).status_code == 200

    other_client = Client()
    other_client.force_login(other_admin)
    assert other_client.get(reverse("admin:index")).status_code == 403


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_business_owner_email_cannot_create_a_guest_password_account(db):
    form = CustomerRegistrationForm(
        data={
            "first_name": "Saeed",
            "last_name": "Owner",
            "email": OWNER_EMAIL,
            "password1": "guest-password-123",
            "password2": "guest-password-123",
            "accept_terms": True,
        }
    )

    assert form.is_valid() is False
    assert "email" in form.errors


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_owner_provisioning_creates_a_google_ready_account_without_password(db):
    call_command("provision_operations_owner")

    owner = get_user_model().objects.get(email=OWNER_EMAIL)
    assert owner.is_staff is True
    assert owner.is_superuser is True
    assert owner.has_usable_password() is False
    assert is_operations_owner(owner) is True


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_owner_provisioning_can_revoke_legacy_administration_roles(db):
    legacy_admin = _admin("legacy@example.invalid", "legacy-admin")
    call_command("provision_operations_owner", "--revoke-other-admin-access")

    legacy_admin.refresh_from_db()
    assert legacy_admin.is_staff is False
    assert legacy_admin.is_superuser is False


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_only_a_verified_google_identity_can_provision_the_owner(db):
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin

    request = RequestFactory().get("/accounts/google/login/callback/")
    SessionMiddleware(lambda _request: None).process_request(request)
    request.session.save()
    request.user = AnonymousUser()
    # The adapter normally receives this from environment-only configuration.
    # The isolated test creates a harmless local stand-in so allauth can resolve
    # the provider while the real provider remains disabled in test settings.
    SocialApp.objects.create(
        provider="google",
        name="Test Google",
        client_id="test-client-id",
        secret="test-client-secret",
    )
    social_login = SocialLogin(
        user=get_user_model()(username="untrusted", email=OWNER_EMAIL),
        account=SocialAccount(
            provider="google",
            uid="verified-google-subject",
            extra_data={"email": OWNER_EMAIL, "email_verified": True},
        ),
        email_addresses=[EmailAddress(email=OWNER_EMAIL, verified=True, primary=True)],
    )

    LuxurySocialAccountAdapter().pre_social_login(request, social_login)

    owner = get_user_model().objects.get(email=OWNER_EMAIL)
    assert social_login.user == owner
    assert is_operations_owner(owner) is True
    assert SocialAccount.objects.filter(user=owner, provider="google").exists()
    verified_address = EmailAddress.objects.get(user=owner, email=OWNER_EMAIL)
    assert verified_address.verified is True
    assert verified_address.primary is True
