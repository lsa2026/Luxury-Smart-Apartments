"""The unified guest entry must preserve existing authentication and booking access."""

import gettext
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils.html import escape

LABELS = {
    "ar": ("حجوزاتي ومكافآتي", "إدارة الحجوزات وبرنامج الولاء"),
    "en": ("My bookings & rewards", "Bookings & loyalty rewards"),
    "fr": ("Mes réservations et avantages", "Réservations et programme de fidélité"),
}


class NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.navigation = None
        self.links = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "nav":
            self.navigation = len(self.links)
            self.links[self.navigation] = []
        elif tag == "a" and self.navigation is not None:
            self.links[self.navigation].append(attrs)

    def handle_endtag(self, tag):
        if tag == "nav":
            self.navigation = None


@pytest.mark.parametrize("language", LABELS)
def test_shipped_catalog_contains_unified_labels(language):
    path = Path(settings.BASE_DIR) / "locale" / language / "LC_MESSAGES/django.mo"
    with path.open("rb") as source:
        catalog = gettext.GNUTranslations(source)
    label, heading = LABELS[language]
    assert catalog.gettext("My bookings & rewards") == label
    assert catalog.gettext("Bookings & loyalty rewards") == heading


@pytest.mark.django_db
@pytest.mark.parametrize("language", LABELS)
@pytest.mark.parametrize("authenticated", [False, True])
def test_desktop_and_mobile_have_one_guest_entry(language, authenticated):
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    if authenticated:
        user = get_user_model().objects.create_user(
            username="navigation-guest", email="navigation@example.invalid"
        )
        client.force_login(user)
    response = client.get(f"/{language}/")
    assert response.status_code == 200
    content = response.content.decode()
    parser = NavigationParser()
    parser.feed(content)
    guest_menus = [
        links
        for links in parser.links.values()
        if any("data-guest-account-link" in link for link in links)
    ]
    assert len(guest_menus) == 2
    for links in guest_menus:
        assert sum("data-guest-account-link" in link for link in links) == 1
        assert sum(link.get("href") == "/my-bookings/" for link in links) == 1
        assert not any(link.get("href") in {"/reservations/manage/", "/login/"} for link in links)
    assert content.count(escape(LABELS[language][0])) == 2


@pytest.mark.django_db
@pytest.mark.parametrize("language", LABELS)
@override_settings(
    GOOGLE_SIGN_IN_ENABLED=True,
    APPLE_SIGN_IN_ENABLED=True,
    SOCIALACCOUNT_PROVIDERS={
        "google": {"APP": {"client_id": "test-only", "secret": "test-only"}},
        "apple": {"APP": {"client_id": "test-only", "secret": "test-only", "key": "test"}},
    },
)
def test_anonymous_entry_preserves_email_social_and_no_account_access(language):
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.get("/my-bookings/", follow=True)
    assert response.status_code == 200
    assert response.redirect_chain == [("/login/?next=/my-bookings/", 302)]
    content = response.content.decode()
    assert 'name="email"' in content
    assert 'name="next" value="/my-bookings/"' in content
    assert 'href="/reservations/manage/"' in content
    for provider in ("google", "apple"):
        assert f'action="/accounts/{provider}/login/?' in content
        assert "next=%2Fmy-bookings%2F" in content
    assert 'method="post"' in content
    assert 'name="csrfmiddlewaretoken"' in content


@pytest.mark.django_db
@pytest.mark.parametrize("language", LABELS)
def test_authenticated_dashboard_keeps_booking_access_and_loyalty_preview(language):
    user = get_user_model().objects.create_user(
        username="dashboard-guest", email="dashboard@example.invalid"
    )
    client = Client()
    client.force_login(user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.get("/my-bookings/")
    assert response.status_code == 200
    content = response.content.decode()
    assert f"<h1>{escape(LABELS[language][1])}</h1>" in content
    assert 'class="loyalty-preview"' in content
    assert 'href="/reservations/manage/"' in content
    assert 'id="upcoming-bookings-heading"' in content
    assert 'id="past-bookings-heading"' in content
    assert 'id="cancelled-bookings-heading"' in content
