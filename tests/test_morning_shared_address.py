"""Same checkout address component for morning guests and operations, fake provider only."""

from urllib.parse import urlsplit

import pytest
from django.test import Client
from django.urls import reverse

from apps.payments.models import MorningPaymentAttempt
from apps.payments.morning_payments import public_url
from apps.reservations.models import Reservation
from tests.test_checkout_address_places import google as _google_provider
from tests.test_checkout_address_places import isolated_places as isolated_places
from tests.test_morning_stays import create
from tests.test_morning_stays import isolated as isolated
from tests.test_reservation_payment_links import billing

pytestmark = pytest.mark.django_db


@pytest.fixture
def google():
    yield from _google_provider.__wrapped__()


def guest_page(client, stay):
    return client.get(urlsplit(public_url(stay)).path)


def send(client, url, ticket, **values):
    return client.post(url, {"ticket": ticket, **values}, content_type="application/json")


def test_guest_selects_address_with_existing_component_and_no_payment_write(google):
    stay, client = create(), Client()
    page = guest_page(client, stay)
    assert page.status_code == 200
    assert page.context["address_places_enabled"]
    assert page.content.count(b"data-checkout-address hidden") == 1
    assert b"css/checkout-address.css" in page.content
    assert b"js/checkout-address.js" in page.content
    ticket = page.context["address_places_ticket"]
    search_url, details_url = (
        page.context["address_suggestions_url"],
        page.context["address_details_url"],
    )
    result = send(client, search_url, ticket, input="10 King Street")
    assert result.status_code == 200
    details = send(client, details_url, ticket, place_id="ChIJ-test_123")
    assert details.status_code == 200
    assert "London" in details.content.decode()
    assert "GB" in details.content.decode()
    assert "SW1A 1AA" in details.content.decode()
    assert google.post.call_args.kwargs["json"]["languageCode"] == "ar"
    assert str(stay.pk) not in str(google.post.call_args)
    assert stay.guest_email not in str(google.post.call_args)
    assert public_url(stay) not in str(google.post.call_args)
    assert send(client, details_url, ticket, place_id="ChIJ-test_123").status_code == 409
    assert google.get.call_count == 1
    assert not MorningPaymentAttempt.objects.exists() and not Reservation.objects.exists()


def test_guest_ticket_cannot_cross_browser_sessions_or_payment_links(google):
    stay, client = create(), Client()
    page = guest_page(client, stay)
    assert (
        send(
            Client(),
            page.context["address_suggestions_url"],
            page.context["address_places_ticket"],
            input="Test road",
        ).status_code
        == 400
    )
    import copy
    import uuid

    other = copy.copy(stay)
    other.pk, other.request_id = uuid.uuid4(), uuid.uuid4()
    other._state = copy.copy(stay._state)
    other._state.adding = True
    other.save(force_insert=True)
    other_url = reverse(
        "payments:morning_address_suggestions",
        args=[urlsplit(public_url(other)).path.rstrip("/").split("/")[-1]],
    )
    assert (
        send(
            client, other_url, page.context["address_places_ticket"], input="Test road"
        ).status_code
        == 400
    )
    google.post.assert_not_called()


@pytest.mark.parametrize("status", ["cancelled", "paid", "review"])
def test_closed_collection_cannot_use_address_provider(google, status):
    stay, client = create(), Client()
    page = guest_page(client, stay)
    stay.status = status
    stay.save()
    assert (
        send(
            client,
            page.context["address_suggestions_url"],
            page.context["address_places_ticket"],
            input="Test road",
        ).status_code
        == 404
    )
    google.post.assert_not_called()


def test_manager_address_is_review_only_for_guest(google):
    stay, client = create(), Client()
    stay.billing_address = billing()
    stay.save()
    page = guest_page(client, stay)
    assert not page.context["address_places_enabled"]
    assert b"data-checkout-address" not in page.content
    assert b'name="billing_street1"' not in page.content
    token = urlsplit(public_url(stay)).path.rstrip("/").split("/")[-1]
    assert (
        send(
            client,
            reverse("payments:morning_address_suggestions", args=[token]),
            "invalid",
            input="Test road",
        ).status_code
        == 404
    )
    google.post.assert_not_called()


def test_disabled_search_keeps_complete_manual_checkout(settings):
    settings.GOOGLE_PLACES_ENABLED = False
    page = guest_page(Client(), create())
    assert not page.context["address_places_enabled"]
    for name in billing():
        assert f'name="{name}"'.encode() in page.content
    assert b'name="terms_accepted"' in page.content
    assert b'name="house_rules_accepted"' in page.content


def test_admin_create_detail_and_validation_error_share_component(google):
    stay, client = create(), Client()
    client.force_login(stay.created_by)
    for url in [
        reverse("notifications:morning_create"),
        reverse("notifications:morning_detail", args=[stay.pk]),
    ]:
        page = client.get(url)
        assert page.content.count(b"data-checkout-address hidden") == 1
        assert b"css/checkout-address.css" in page.content
        assert b"js/checkout-address.js" in page.content
        result = send(
            client,
            page.context["address_suggestions_url"],
            page.context["address_places_ticket"],
            input="10 King Street",
        )
        assert result.status_code == 200
    error = client.post(
        reverse("notifications:morning_detail", args=[stay.pk]),
        {"action": "save_billing", "billing_country": "SA"},
    )
    assert error.status_code == 200
    assert error.content.count(b"data-checkout-address hidden") == 1
    assert (
        b"js/checkout-address.js" in error.content and b"css/checkout-address.css" in error.content
    )
    assert not MorningPaymentAttempt.objects.exists()


def test_search_csrf_and_role_checks_precede_provider(google):
    stay = create()
    client = Client(enforce_csrf_checks=True)
    client.force_login(stay.created_by)
    page = client.get(reverse("notifications:morning_detail", args=[stay.pk]))
    url, ticket = page.context["address_suggestions_url"], page.context["address_places_ticket"]
    assert send(client, url, ticket, input="10 King Street").status_code == 403
    assert Client().post(url, {}, content_type="application/json").status_code in {302, 403}
    guest = Client(enforce_csrf_checks=True)
    gpage = guest_page(guest, stay)
    assert (
        send(
            guest,
            gpage.context["address_suggestions_url"],
            gpage.context["address_places_ticket"],
            input="10 King Street",
        ).status_code
        == 403
    )
    google.post.assert_not_called()
