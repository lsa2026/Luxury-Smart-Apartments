"""Short catalogue URLs preserve apartment/language and cannot redirect elsewhere."""

from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import translation

from apps.properties.models import Property, PropertyImage
from apps.reservations.models import BookingIntent, BookingQuote, Reservation

pytestmark = pytest.mark.django_db

CATALOGUE = [
    ("e12", 315816, "luxury-smart-apartment-e12"),
    ("irqah", 315814, "spacious-and-modern-apartment-for-rent-in-riyadh"),
    ("darat-safa", 315815, "darat-safa-luxury-apartment"),
    ("a11", 325961, "luxury-smart-apartment-a11"),
    ("b12", 343666, "luxury-smart-apartment-b12"),
    ("nour-prestige", 511786, "luxury-smart-apartment-at-nour-prestige-marrakech"),
]


def apartment(listing_id=315816, slug="luxury-smart-apartment-e12", **fields):
    return Property.objects.create(
        hostaway_listing_id=listing_id,
        slug=slug,
        name_ar="شقة اختبار",
        name_en="Synthetic apartment",
        name_fr="Appartement de test",
        **fields,
    )


@pytest.mark.parametrize("code,listing_id,slug", CATALOGUE)
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("photos", [False, True])
def test_catalogue_targets_exact_listing_and_explicit_language(
    code, listing_id, slug, language, photos, settings
):
    place = apartment(listing_id, slug)
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr" if language != "fr" else "ar"
    suffix = "photos/" if photos else ""
    with patch(
        "apps.integrations.hostaway.client.HostawayClient",
        side_effect=AssertionError("Short links must not call Hostaway or quote prices"),
    ):
        response = client.get(f"/{language}/p/{code}/{suffix}", HTTP_ACCEPT_LANGUAGE="en")
    target = f"/{language}/properties/{slug}/" + ("gallery/" if photos else "#price-calendar")
    assert response.status_code == 302
    assert response["Location"] == target
    assert response["Cache-Control"] == "no-store"
    assert response["X-Robots-Tag"] == "noindex, follow"
    assert not BookingQuote.objects.exists()
    assert not BookingIntent.objects.exists()
    assert not Reservation.objects.exists()
    place.refresh_from_db()
    assert place.slug == slug


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("photos", [False, True])
def test_short_links_ignore_external_targets_and_all_query_values(method, photos):
    apartment()
    suffix = "photos/" if photos else ""
    response = getattr(Client(), method)(
        f"/ar/p/e12/{suffix}?next=https://evil.invalid&target=//evil.invalid"
        "&url=https://evil.invalid&language=fr&check_in=2027-01-01&guests=999"
    )
    assert response.status_code == 302
    assert response["Location"] == "/ar/properties/luxury-smart-apartment-e12/" + (
        "gallery/" if photos else "#price-calendar"
    )


@pytest.mark.parametrize("photos", [False, True])
@pytest.mark.parametrize("fields", [{"is_visible": False}, {"hostaway_is_active": False}])
def test_short_link_cannot_expose_a_hidden_or_inactive_apartment(fields, photos):
    apartment(**fields)
    suffix = "photos/" if photos else ""
    response = Client().get(f"/ar/p/e12/{suffix}")
    assert response.status_code == 404
    assert "Location" not in response


@pytest.mark.parametrize("code", ["unknown", "E12", "villa", "325731"])
def test_unknown_or_unapproved_codes_do_not_fall_back_to_other_properties(code):
    apartment()
    apartment(325731, "synthetic-villa")
    response = Client().get(f"/ar/p/{code}/?target=/ar/p/e12/")
    assert response.status_code == 404
    assert "Location" not in response


def test_missing_approved_listing_does_not_match_an_unrelated_slug():
    apartment(999111, "e12")
    response = Client().get("/ar/p/e12/")
    assert response.status_code == 404
    assert "Location" not in response


def test_current_slug_is_resolved_without_a_database_migration_or_cached_destination():
    place = apartment()
    assert (
        Client()
        .get("/en/p/e12/")["Location"]
        .endswith("/luxury-smart-apartment-e12/#price-calendar")
    )
    Property.objects.filter(pk=place.pk).update(slug="renamed-synthetic-e12")
    assert Client().get("/en/p/e12/")["Location"] == (
        "/en/properties/renamed-synthetic-e12/#price-calendar"
    )
    Property.objects.filter(pk=place.pk).update(is_visible=False)
    assert Client().get("/en/p/e12/").status_code == 404


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_short_link_does_not_accept_mutating_methods(method):
    apartment()
    response = getattr(Client(), method)("/ar/p/e12/")
    assert response.status_code == 405
    assert response["Allow"] == "GET, HEAD"


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_short_routes_reverse_in_each_language(language):
    with translation.override(language):
        assert reverse("property_short_calendar", kwargs={"code": "e12"}) == (f"/{language}/p/e12/")
        assert reverse("property_short_gallery", kwargs={"code": "e12"}) == (
            f"/{language}/p/e12/photos/"
        )


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("photos", [False, True])
def test_following_short_link_reaches_existing_calendar_or_gallery_without_a_quote(
    language, photos
):
    place = apartment()
    PropertyImage.objects.create(property=place, image="synthetic-short-link.jpg")
    suffix = "photos/" if photos else ""
    with patch(
        "apps.integrations.hostaway.client.HostawayClient",
        side_effect=AssertionError("Following a catalogue link cannot request a live quote"),
    ):
        response = Client().get(f"/{language}/p/e12/{suffix}", follow=True)
    assert response.status_code == 200
    assert len(response.redirect_chain) == 1
    html = response.content.decode()
    assert f'<html lang="{language}"' in html
    assert ("gallery/" if photos else "#price-calendar") in response.redirect_chain[0][0]
    assert ("synthetic-short-link.jpg" if photos else 'id="price-calendar"') in html
    assert not BookingQuote.objects.exists()
    assert not BookingIntent.objects.exists()
    assert not Reservation.objects.exists()


@pytest.mark.parametrize("url", ["/p/e12/", "/de/p/e12/", "/ar/p/e12/other/"])
def test_short_links_do_not_invent_a_language_or_another_destination(url):
    apartment()
    response = Client().get(url)
    assert response.status_code == 404
    assert "Location" not in response
