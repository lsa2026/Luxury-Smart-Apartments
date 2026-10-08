"""Account layout, privacy and profile regressions; no external provider calls."""

from html import unescape

import pytest
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.accounts.dashboard_copy import ACCOUNT_COPY
from apps.accounts.models import profile_for
from apps.reservations.models import Reservation
from tests.test_account_booking_claim import make_reservation

pytestmark = pytest.mark.django_db


@pytest.fixture
def guest_client():
    user = get_user_model().objects.create_user(
        username="account-guest",
        email="guest@example.invalid",
        first_name="أحمد",
    )
    profile_for(user).mark_verified()
    client = Client()
    client.force_login(user)
    return user, client


@pytest.mark.parametrize(
    "path", ["/my-bookings/", "/my-bookings/rewards/", "/my-bookings/details/"]
)
def test_account_pages_require_sign_in(path):
    response = Client().get(path)
    assert response.status_code == 302
    assert response.url == f"/login/?next={path}"


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("section", ["", "rewards/", "details/"])
def test_account_pages_localized_private_and_not_duplicated(guest_client, language, section):
    user, client = guest_client
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.get(f"/my-bookings/{section}")
    assert response.status_code == 200
    content = unescape(response.content.decode())
    copy = ACCOUNT_COPY[language]
    assert copy["welcome_named"] % {"name": user.first_name} in content
    assert copy["welcome_back"] in content
    assert "no-store" in response["Cache-Control"]
    assert 'content="noindex,nofollow"' in content
    assert "whatsapp-fab" not in content
    assert 'class="site-footer"' not in content
    assert "css/guest-account.css" in content
    assert 'action="/logout/"' in content
    assert 'name="csrfmiddlewaretoken"' in content
    assert "سجل الحجوزات" not in content
    assert 'class="loyalty-preview"' not in content
    if section == "rewards/":
        assert copy["rewards_intro"] in content
        assert copy["rewards_hint"] in content
        assert 'id="account-bookings-heading"' not in content
        assert 'id="account-details-heading"' not in content
    elif section == "details/":
        assert copy["personal"] in content
        assert copy["address_hint"] in content
        assert 'id="account-rewards-heading"' not in content
        assert 'id="account-bookings-heading"' not in content
        for field in response.context["profile_form"]:
            assert field.label in content
    else:
        assert copy["choose_stay"] in content
        assert 'id="account-rewards-heading"' not in content
        assert 'name="phone"' not in content
    assert set(ACCOUNT_COPY[language]) == set(ACCOUNT_COPY["en"])


def test_filters_only_render_selected_owned_reservations(guest_client):
    user, client = guest_client
    upcoming = make_reservation("REDESIGN-UPCOMING")
    past = make_reservation("REDESIGN-PAST")
    cancelled = make_reservation("REDESIGN-CANCELLED")
    other = make_reservation("REDESIGN-OTHER")
    for reservation in (upcoming, past, cancelled):
        reservation.booking_intent.customer = user
        reservation.booking_intent.save(update_fields=["customer"])
    past.check_in = timezone.localdate() - timezone.timedelta(days=3)
    past.check_out = timezone.localdate() - timezone.timedelta(days=1)
    past.save(update_fields=["check_in", "check_out"])
    cancelled.normalized_status = Reservation.Status.CANCELLED
    cancelled.save(update_fields=["normalized_status"])
    for selected, expected in (("upcoming", upcoming), ("past", past), ("cancelled", cancelled)):
        response = client.get("/my-bookings/", {"filter": selected})
        assert list(response.context["booking_page"]) == [expected]
        assert response.context["booking_counts"] == {"upcoming": 1, "past": 1, "cancelled": 1}
        content = response.content.decode()
        for reservation in (upcoming, past, cancelled, other):
            assert (reservation.public_reference in content) == (reservation == expected)
        assert f"/reservations/manage/{expected.public_reference}/" in content


def test_invalid_filter_defaults_to_upcoming_and_history_is_paginated(guest_client):
    user, client = guest_client
    for index in range(8):
        reservation = make_reservation(f"REDESIGN-PAGED-{index}")
        reservation.booking_intent.customer = user
        reservation.booking_intent.save(update_fields=["customer"])
    response = client.get("/my-bookings/", {"filter": "<script>", "page": "invalid"})
    assert response.context["booking_filter"] == "upcoming"
    assert len(response.context["booking_page"]) == 6
    assert response.context["booking_counts"]["upcoming"] == 8
    response = client.get("/my-bookings/", {"page": 2})
    assert len(response.context["booking_page"]) == 2


def test_profile_saves_optional_fields_but_cannot_change_identity(guest_client):
    user, client = guest_client
    response = client.post(
        "/my-bookings/details/",
        {
            "first_name": "Layla",
            "last_name": "Guest",
            "phone": "+966501234567",
            "preferred_language": "fr",
            "email": "attacker@example.invalid",
            "user_id": "999",
        },
    )
    assert response.status_code == 302
    assert response.url == "/my-bookings/details/"
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "fr"
    user.refresh_from_db()
    profile = profile_for(user)
    assert user.email == "guest@example.invalid"
    assert user.first_name == "Layla"
    assert profile.phone == "+966501234567"
    assert profile.preferred_language == "fr"
    assert profile.residence_country == ""
    assert profile.is_email_verified


def test_invalid_language_and_phone_display_errors_without_partial_save(guest_client):
    user, client = guest_client
    response = client.post(
        "/my-bookings/details/",
        {
            "first_name": "Should not save",
            "phone": "0501234567",
            "preferred_language": "xx",
        },
    )
    assert response.status_code == 200
    assert response.context["account_section"] == "details"
    assert set(response.context["profile_form"].errors) == {"phone", "preferred_language"}
    user.refresh_from_db()
    assert user.first_name == "أحمد"


def test_profile_email_is_readonly_and_only_actual_linked_providers_are_shown(guest_client):
    user, client = guest_client
    SocialAccount.objects.create(user=user, provider="google", uid="redesign-google-only")
    response = client.get("/my-bookings/details/")
    assert response.context["sign_in_methods"][-1] == "Google"
    assert "Apple" not in response.context["sign_in_methods"]
    assert 'readonly aria-describedby="account-email-help"' in response.content.decode()


def test_rewards_page_is_readonly_and_does_not_issue_points(guest_client):
    _, client = guest_client
    response = client.post("/my-bookings/rewards/", {"points": 1000})
    assert response.status_code == 405


def test_greeting_is_escaped_and_last_login_is_real(guest_client):
    user, client = guest_client
    user.first_name = '<img src=x onerror="alert(1)">'
    user.save(update_fields=["first_name"])
    response = client.get("/my-bookings/")
    content = response.content.decode()
    assert '<img src=x onerror="alert(1)">' not in content
    assert "&lt;img" in content
    user.refresh_from_db()
    assert response.context["user"].last_login == user.last_login
    assert '<time datetime="' in content


def test_profile_post_requires_csrf_and_logout_remains_post_only(guest_client):
    user, _ = guest_client
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post("/my-bookings/details/", {"first_name": "Changed"}).status_code == 403
    assert client.get("/logout/").status_code == 405
