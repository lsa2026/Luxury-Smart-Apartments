"""Fictional guests, optional addresses and mocked provider only."""

import json
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.access_copy import COPY
from apps.accounts.models import profile_for
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


@pytest.fixture
def guest():
    user = get_user_model().objects.create_user(
        username="address-preview", email="address@example.invalid", first_name="ضيف"
    )
    profile_for(user).mark_verified()
    client = Client()
    client.force_login(user)
    return user, client


@pytest.fixture
def google(settings):
    settings.GOOGLE_PLACES_ENABLED = True
    settings.GOOGLE_PLACES_API_KEY = "fictional-provider-key"
    settings.GOOGLE_PLACES_REQUIRE_SHARED_CACHE = False
    settings.GOOGLE_PLACES_DAILY_LIMIT = 300
    from django.core.cache import cache

    cache.clear()
    with patch("apps.reservations.address_places.httpx.Client") as factory:
        provider = factory.return_value.__enter__.return_value
        provider.post.return_value.json.return_value = {
            "suggestions": [
                {
                    "placePrediction": {
                        "placeId": "ChIJ-fictional",
                        "text": {"text": "Fictional Street"},
                    }
                }
            ]
        }
        provider.get.return_value.json.return_value = {
            "addressComponents": [
                {"types": ["route"], "longText": "Fictional Street"},
                {"types": ["locality"], "longText": "Test City"},
                {"types": ["country"], "longText": "Canada", "shortText": "CA"},
            ]
        }
        yield provider
    cache.clear()


def search(client, ticket, *, details=False, **body):
    path = "address-details" if details else "address-suggestions"
    return client.post(
        f"/my-bookings/details/{path}/",
        data=json.dumps({"ticket": ticket, **body}),
        content_type="application/json",
    )


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_login_is_compact_loyalty_aware_and_keeps_authentication(language):
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.get("/login/?next=/my-bookings/rewards/")
    html = response.content.decode()
    assert COPY[language]["heading"] in html
    assert COPY[language]["note"] in html
    assert "guest-access.css" in html
    assert "photo-1674386491555" not in html and "whatsapp-fab" not in html
    assert 'name="email"' in html and 'name="next" value="/my-bookings/rewards/"' in html
    assert 'name="csrfmiddlewaretoken"' in html
    assert set(COPY[language]) == set(COPY["ar"])


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_email_pending_has_one_inline_notice_and_keeps_the_code_route(guest, language):
    user, _ = guest
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    assert client.post("/login/", {"email": user.email}).status_code == 302
    response = client.get("/account/confirm/")
    html = response.content.decode()
    assert html.count('class="container alerts"') == 1
    assert html.index('class="auth-panel"') < html.index('class="container alerts"')
    assert 'class="guest-verification"' in html and 'class="auth-aside"' not in html
    assert 'action="/account/confirm/code/"' in html
    assert 'action="/account/confirm/resend/"' in html
    assert 'name="code"' in html and 'autocomplete="one-time-code"' in html
    assert 'dir="ltr"' in html and user.email in html
    assert COPY[language]["verify_hint"] in html
    assert "whatsapp-fab" not in html
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("country", ["", "US", "السعودية", "United Kingdom"])
def test_optional_country_select_preserves_existing_values_without_a_default(guest, country):
    user, client = guest
    profile = profile_for(user)
    profile.residence_country = country
    profile.save(update_fields=["residence_country"])
    response = client.get("/my-bookings/details/")
    form = response.context["profile_form"]
    assert form["residence_country"].value() == country
    assert country in dict(form.fields["residence_country"].choices)
    assert not form.fields["residence_country"].required
    assert "data-country-select" in response.content.decode()
    assert client.post("/my-bookings/details/", {"residence_country": country}).status_code == 302
    assert profile_for(user).residence_country == country


def test_account_lookup_needs_no_booking_and_does_not_save_profile(guest, google):
    user, client = guest
    before = profile_for(user).residence_city
    page = client.get("/my-bookings/details/")
    assert 'data-address-profile="true"' in page.content.decode()
    assert (
        COPY["ar"]["address_disclosure"]
        in client.get("/my-bookings/details/", HTTP_ACCEPT_LANGUAGE="ar").content.decode()
    )
    google.post.assert_not_called()  # No paid/provider call on page load.
    ticket = page.context["address_places_ticket"]
    result = search(client, ticket, input="Fictional Street")
    assert result.status_code == 200 and "no-store" in result["Cache-Control"]
    details = search(client, ticket, details=True, place_id="ChIJ-fictional")
    assert details.json()["fields"]["billing_country"] == "CA"
    assert details.json()["fields"]["billing_city"] == "Test City"
    assert profile_for(user).residence_city == before
    assert not Reservation.objects.exists()
    assert search(client, ticket, details=True, place_id="ChIJ-fictional").status_code == 409
    assert google.get.call_count == 1


def test_lookup_auth_csrf_tampering_and_cross_session_never_call_provider(guest, google):
    user, client = guest
    ticket = client.get("/my-bookings/details/").context["address_places_ticket"]
    assert search(Client(), ticket, input="Fictional Street").status_code == 302
    other = Client()
    other.force_login(user)
    assert search(other, ticket, input="Fictional Street").status_code == 400
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    assert search(strict, ticket, input="Fictional Street").status_code == 403
    assert search(client, "tampered", input="Fictional Street").status_code == 400
    assert search(client, ticket, details=True, place_id="unselected").status_code == 400
    google.post.assert_not_called()
    google.get.assert_not_called()


def test_account_tickets_cannot_cross_accounts_or_checkout_namespace(guest, google):
    from django.test import RequestFactory

    from apps.accounts.address_places import account_address_ticket
    from apps.reservations.address_places import make_address_ticket
    from tests.test_booking_views_admin import owned_client_quote
    from tests.test_checkout_address_places import post

    user, client = guest
    # Persist the browser marker first: the only changed identity below is
    # the account, not an accidentally new session.
    client.get("/my-bookings/details/")
    request = RequestFactory().get("/my-bookings/details/")
    request.session = client.session
    request.user = user
    request.user = get_user_model().objects.create_user(username="other-address")
    different_account_ticket = account_address_ticket(request)
    assert search(client, different_account_ticket, input="Fictional Street").status_code == 400

    checkout_client, quote, reference = owned_client_quote()
    checkout_client.force_login(user)
    ticket = checkout_client.get("/my-bookings/details/").context["address_places_ticket"]
    assert post(checkout_client, reference, ticket, input="Fictional Street").status_code == 400
    assert (
        search(checkout_client, make_address_ticket(quote), input="Fictional Street").status_code
        == 400
    )
    google.post.assert_not_called()
    google.get.assert_not_called()


def test_disabled_search_keeps_manual_address_and_no_lazy_script(guest, google, settings):
    settings.GOOGLE_PLACES_ENABLED = False
    _, client = guest
    response = client.get("/my-bookings/details/")
    assert "checkout-address.js" not in response.content.decode()
    assert 'name="residence_country"' in response.content.decode()
    assert search(client, "unused", input="Fictional Street").status_code == 503
    google.post.assert_not_called()


def test_address_search_scripts_are_not_added_to_other_account_sections(guest, google):
    _, client = guest
    for path in ["/my-bookings/", "/my-bookings/rewards/"]:
        assert "checkout-address.js" not in client.get(path).content.decode()
    google.post.assert_not_called()
