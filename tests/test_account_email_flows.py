"""Email confirmation and password reset, including what each must refuse."""

from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.accounts.models import CustomerProfile, profile_for
from apps.accounts.tokens import make_verification_token, read_verification_token
from apps.notifications.models import EmailDelivery
from apps.notifications.services.email import TEMPLATE_GROUPS

pytestmark = pytest.mark.django_db

User = get_user_model()

REGISTRATION = {
    "first_name": "Nora",
    "last_name": "Example",
    "email": "nora@example.invalid",
    "password1": "Correct-Horse-Battery-2026",
    "password2": "Correct-Horse-Battery-2026",
    "accept_terms": "on",
}


def make_user(email: str = "nora@example.invalid") -> User:
    return User.objects.create_user(
        username=email,
        email=email,
        password="Correct-Horse-Battery-2026",
        first_name="Nora",
    )


# --- verification -----------------------------------------------------------


def test_registering_queues_a_confirmation_email() -> None:
    Client().post("/register/", REGISTRATION)

    delivery = EmailDelivery.objects.get(message_type="account_verify_email")
    assert delivery.recipient_source == "account"
    assert delivery.template_name == "account"
    # The address is masked and hashed rather than stored in the clear.
    assert "nora@example.invalid" not in delivery.recipient_masked


def test_a_new_account_starts_unverified() -> None:
    Client().post("/register/", REGISTRATION)

    profile = CustomerProfile.objects.get(user__email="nora@example.invalid")
    assert profile.is_email_verified is False


def test_opening_the_link_confirms_the_address() -> None:
    user = make_user()
    token = make_verification_token(user.pk, user.email)

    response = Client().get(f"/account/verify/{token}/", follow=True)

    assert response.status_code == 200
    assert profile_for(user).is_email_verified is True


def test_confirming_works_from_any_browser() -> None:
    """Mail clients open links in whichever browser they like."""
    user = make_user()
    token = make_verification_token(user.pk, user.email)

    # A client that never signed in.
    Client().get(f"/account/verify/{token}/", follow=True)

    assert profile_for(user).is_email_verified is True


def test_a_link_issued_for_an_old_address_cannot_confirm_a_new_one() -> None:
    user = make_user()
    token = make_verification_token(user.pk, user.email)
    user.email = "changed@example.invalid"
    user.save(update_fields=["email"])

    Client().get(f"/account/verify/{token}/", follow=True)

    assert profile_for(user).is_email_verified is False


def test_a_tampered_token_confirms_nothing() -> None:
    user = make_user()
    token = make_verification_token(user.pk, user.email)

    Client().get(f"/account/verify/{token[:-3]}xyz/", follow=True)

    assert profile_for(user).is_email_verified is False


@override_settings(ACCOUNT_VERIFICATION_MAX_AGE_SECONDS=-1)
def test_an_expired_token_is_rejected() -> None:
    user = make_user()
    token = make_verification_token(user.pk, user.email)

    assert read_verification_token(token) is None


def test_confirming_queues_the_welcome_email_once() -> None:
    user = make_user()
    token = make_verification_token(user.pk, user.email)

    Client().get(f"/account/verify/{token}/", follow=True)
    Client().get(f"/account/verify/{token}/", follow=True)

    assert EmailDelivery.objects.filter(message_type="account_welcome").count() == 1


# --- password reset ---------------------------------------------------------


def test_requesting_a_reset_queues_it_through_the_site_pipeline() -> None:
    make_user()

    Client().post("/account/reset/", {"email": "nora@example.invalid"})

    delivery = EmailDelivery.objects.get(message_type="account_password_reset")
    assert delivery.recipient_source == "account"


def test_an_unknown_address_is_answered_the_same_way() -> None:
    """Whether an account exists must not leak from the response."""
    known = Client().post("/account/reset/", {"email": "nora@example.invalid"})
    make_user()
    unknown = Client().post("/account/reset/", {"email": "nobody@example.invalid"})

    assert known.status_code == unknown.status_code
    assert known.url == unknown.url


def test_no_reset_is_queued_for_an_address_without_an_account() -> None:
    Client().post("/account/reset/", {"email": "nobody@example.invalid"})

    assert EmailDelivery.objects.filter(message_type="account_password_reset").count() == 0


def test_the_reset_pages_are_reachable() -> None:
    client = Client()

    assert client.get("/account/reset/").status_code == 200
    assert client.get("/account/reset/sent/").status_code == 200
    assert client.get("/account/reset/done/").status_code == 200


# --- template coverage ------------------------------------------------------


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("group", sorted(set(TEMPLATE_GROUPS.values())))
def test_every_message_group_has_a_template_in_every_language(
    group: str,
    language: str,
) -> None:
    """A group registered without its templates would fail only at send time."""
    template = Path(settings.BASE_DIR) / "templates" / "emails" / language / f"{group}.html"

    assert template.is_file(), f"missing {template.relative_to(settings.BASE_DIR)}"
