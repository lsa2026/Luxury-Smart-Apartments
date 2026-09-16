from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone, translation

from apps.core.templatetags.presentation import (
    localized_property_description,
    localized_property_meta_description,
    localized_property_seo_title,
)
from apps.integrations.hostaway.availability_validators import CalendarDay, CalendarDocument
from apps.properties.models import Property, PropertyImage
from apps.reservations.services.availability import CalendarFetch
from apps.reservations.views import PropertyCalendarAvailabilityView

pytestmark = pytest.mark.django_db


def make_property(listing_id: int, *, visible: bool = True, active: bool = True) -> Property:
    return Property.objects.create(
        hostaway_listing_id=listing_id,
        slug=f"property-{listing_id}",
        hostaway_name=f"Source {listing_id}",
        name_ar=f"وحدة {listing_id}",
        name_en=f"Property {listing_id}",
        city_ar="الرياض",
        city_en="Riyadh",
        city="Riyadh",
        country_code="SA",
        currency_code="SAR",
        is_visible=visible,
        hostaway_special_status="" if active else "archived",
    )


def test_hidden_and_inactive_properties_are_not_public() -> None:
    make_property(1)
    make_property(2, visible=False)
    make_property(3, active=False)

    response = Client().get("/ar/properties/")
    content = response.content.decode()

    assert response.status_code == 200
    assert "وحدة 1" in content
    assert "وحدة 2" not in content
    assert "وحدة 3" not in content


def test_hidden_images_are_not_rendered_on_detail() -> None:
    property_obj = make_property(10)
    PropertyImage.objects.create(
        property=property_obj,
        hostaway_image_id=1,
        hostaway_url="https://hostaway.example/visible.jpg",
        sync_key="id:1",
        source=PropertyImage.Source.HOSTAWAY,
        is_visible=True,
    )
    PropertyImage.objects.create(
        property=property_obj,
        hostaway_image_id=2,
        hostaway_url="https://hostaway.example/hidden.jpg",
        sync_key="id:2",
        source=PropertyImage.Source.HOSTAWAY,
        is_visible=False,
    )

    response = Client().get(property_obj.get_absolute_url())
    content = response.content.decode()

    assert "visible.jpg" in content
    assert "hidden.jpg" not in content


def test_property_reviews_use_only_the_property_trustindex_slider() -> None:
    property_obj = make_property(16)
    property_obj.trustindex_widget_id = "c48ff45802b74294b646cc8e842"
    property_obj.trustindex_rating = Decimal("4.8")
    property_obj.trustindex_review_count = 131
    property_obj.save(
        update_fields=[
            "trustindex_widget_id",
            "trustindex_rating",
            "trustindex_review_count",
        ]
    )

    response = Client().get(property_obj.get_absolute_url())
    content = response.content.decode()

    assert response.status_code == 200
    assert "data-trustindex-property-reviews" in content
    assert "cdn.trustindex.io/loader.js?c48ff45802b74294b646cc8e842" in content
    assert "4.8" in content
    assert "131" in content
    assert "https://cdn.trustindex.io" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    ("language", "widget_id"),
    [
        ("ar", "18981608195554449116091f051"),
        ("en", "83d89b481fb85445c336aec8036"),
        ("fr", "5f7b4a781d7d544ef3460596269"),
    ],
)
def test_property_full_review_page_uses_the_checked_list_widget(
    language: str,
    widget_id: str,
) -> None:
    property_obj = make_property(315814)

    response = Client().get(f"/{language}/properties/{property_obj.slug}/reviews/")
    content = response.content.decode()

    assert response.status_code == 200
    assert "data-trustindex-property-full-reviews" in content
    assert f"cdn.trustindex.io/loader.js?{widget_id}" in content
    assert "data-analytics-view-all-reviews" in content
    assert f'data-analytics-language="{language}"' in content
    assert "https://cdn.trustindex.io" in response.headers["Content-Security-Policy"]


def test_property_detail_has_a_clear_link_to_its_full_review_page() -> None:
    property_obj = make_property(315814)
    property_obj.trustindex_widget_id = "c48ff45802b74294b646cc8e842"
    property_obj.trustindex_rating = Decimal("4.8")
    property_obj.trustindex_review_count = 131
    property_obj.save(
        update_fields=[
            "trustindex_widget_id",
            "trustindex_rating",
            "trustindex_review_count",
        ]
    )

    content = Client().get(f"/ar/properties/{property_obj.slug}/").content.decode()

    assert f'href="/ar/properties/{property_obj.slug}/reviews/"' in content
    assert "جميع المراجعات" in content
    assert "131" in content


def test_property_full_review_page_is_not_available_without_a_checked_widget() -> None:
    property_obj = make_property(325731)

    assert Client().get(f"{property_obj.get_absolute_url()}reviews/").status_code == 404


def test_property_without_a_trustindex_widget_does_not_show_a_rating() -> None:
    property_obj = make_property(17)
    property_obj.average_review_rating = Decimal("9.9")
    property_obj.save(update_fields=["average_review_rating"])

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "data-trustindex-property-reviews" not in content
    assert "9.9" not in content


def test_browse_dates_are_reassured_on_the_property_page() -> None:
    property_obj = make_property(13)
    check_in = timezone.localdate() + timedelta(days=10)
    check_out = check_in + timedelta(days=3)

    response = Client().get(
        property_obj.get_absolute_url(),
        {
            "source": "browse",
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "guests": "2",
        },
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert 'class="booking-selection-summary"' in content
    assert "data-mobile-booking-cta" in content


def test_arabic_property_description_never_falls_back_to_hostaway_english() -> None:
    property_obj = make_property(14)
    property_obj.hostaway_description = "Source copy must not appear in Arabic."
    property_obj.save(update_fields=["hostaway_description"])

    with translation.override("ar"):
        assert localized_property_description(property_obj) == ""
    with translation.override("en"):
        assert localized_property_description(property_obj) == property_obj.hostaway_description


def test_french_property_seo_uses_french_content() -> None:
    property_obj = make_property(15)
    property_obj.seo_title_ar = "عنوان عربي"
    property_obj.seo_title_fr = "Titre français"
    property_obj.seo_description_ar = "وصف عربي"
    property_obj.seo_description_fr = "Description française"
    property_obj.save()

    with translation.override("fr"):
        assert localized_property_seo_title(property_obj) == "Titre français"
        assert localized_property_meta_description(property_obj) == "Description française"


class CalendarProjectionService:
    def __enter__(self) -> "CalendarProjectionService":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def fetch_calendar(self, **kwargs: object) -> CalendarFetch:
        start_date = kwargs["start_date"]
        assert isinstance(start_date, type(timezone.localdate()))
        return CalendarFetch(
            CalendarDocument(
                days=(
                    CalendarDay(
                        date=start_date,
                        is_available=False,
                        price=None,
                        minimum_stay=None,
                        maximum_stay=None,
                        closed_on_arrival=False,
                        closed_on_departure=False,
                        status="unavailable",
                    ),
                    CalendarDay(
                        date=start_date + timedelta(days=1),
                        is_available=True,
                        price=None,
                        minimum_stay=None,
                        maximum_stay=None,
                        closed_on_arrival=False,
                        closed_on_departure=True,
                        status="available",
                    ),
                ),
                envelope_fields=frozenset(),
                day_field_types=(),
            ),
            0,
            False,
        )


def test_calendar_projection_exposes_only_safe_day_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    property_obj = make_property(15)
    monkeypatch.setattr(
        PropertyCalendarAvailabilityView,
        "service_class",
        CalendarProjectionService,
    )
    start_date = timezone.localdate()

    response = Client().get(
        f"/reservations/calendar/{property_obj.slug}/",
        {"start": start_date.isoformat(), "end": (start_date + timedelta(days=2)).isoformat()},
    )

    assert response.status_code == 200
    assert response.json()["days"] == [
        {
            "date": start_date.isoformat(),
            "arrival_available": False,
            "departure_available": True,
        },
        {
            "date": (start_date + timedelta(days=1)).isoformat(),
            "arrival_available": True,
            "departure_available": False,
        },
    ]


def test_enabled_public_location_renders_an_interactive_map_and_geo_schema() -> None:
    property_obj = make_property(11)
    property_obj.public_location_enabled = True
    property_obj.public_location_latitude = Decimal("24.713552")
    property_obj.public_location_longitude = Decimal("46.675296")
    property_obj.google_maps_cid = "4981474889453888860"
    property_obj.save(
        update_fields=[
            "public_location_enabled",
            "public_location_latitude",
            "public_location_longitude",
            "google_maps_cid",
        ]
    )

    response = Client().get(property_obj.get_absolute_url())
    content = response.content.decode()

    assert response.status_code == 200
    assert "data-property-map" in content
    assert 'data-latitude="24.713552"' in content
    assert 'data-longitude="46.675296"' in content
    assert "vendor/leaflet/leaflet.js" in content
    assert "google.com/maps?cid=4981474889453888860" in content
    assert '"@type":"GeoCoordinates"' in content
    assert "https://tile.openstreetmap.org" in response.headers["Content-Security-Policy"]


def test_disabled_public_location_does_not_expose_coordinates_or_map_resources() -> None:
    property_obj = make_property(12)
    property_obj.public_location_latitude = Decimal("24.700001")
    property_obj.public_location_longitude = Decimal("46.600001")
    property_obj.save(update_fields=["public_location_latitude", "public_location_longitude"])

    response = Client().get(property_obj.get_absolute_url())
    content = response.content.decode()

    assert "data-property-map" not in content
    assert "24.700001" not in content
    assert "vendor/leaflet/leaflet.js" not in content
    assert '"@type":"GeoCoordinates"' not in content
    assert "https://tile.openstreetmap.org" not in response.headers["Content-Security-Policy"]


def test_property_list_is_paginated() -> None:
    for listing_id in range(20, 31):
        make_property(listing_id)

    first_page = Client().get("/ar/properties/")
    second_page = Client().get("/ar/properties/?page=2")

    assert first_page.context["paginator"].per_page == 9
    assert len(first_page.context["properties"]) == 9
    assert len(second_page.context["properties"]) == 2


def test_property_list_avoids_n_plus_one(django_assert_num_queries: object) -> None:
    for listing_id in range(40, 45):
        property_obj = make_property(listing_id)
        PropertyImage.objects.create(
            property=property_obj,
            image=f"properties/{listing_id}/local.jpg",
            source=PropertyImage.Source.LOCAL,
        )

    with django_assert_num_queries(3):
        response = Client().get("/ar/properties/")
        assert response.status_code == 200


# The same photograph also appears in the featured-property cards further down
# the page, so every assertion below is scoped to the destinations grid.
RIYADH_STOCK_PHOTO = "photo-1674386491555"


def destinations_grid(content: str) -> str:
    start = content.index('class="destination-grid"')
    return content[start : content.index("</section>", start)]


def make_hero_image(
    property_obj: Property,
    url: str,
    *,
    is_city_hero: bool = True,
    is_visible: bool = True,
) -> PropertyImage:
    return PropertyImage.objects.create(
        property=property_obj,
        source=PropertyImage.Source.HOSTAWAY,
        hostaway_url=url,
        hostaway_image_id=abs(hash(url)) % 10_000_000,
        is_city_hero=is_city_hero,
        is_visible=is_visible,
    )


def test_home_page_shows_the_nominated_photo_for_each_city() -> None:
    riyadh = make_property(101)
    marrakesh = make_property(102)
    marrakesh.city = "Marrakesh"
    marrakesh.city_ar = "مراكش"
    marrakesh.save(update_fields=["city", "city_ar"])
    make_hero_image(riyadh, "https://example.invalid/riyadh-hero.jpg")
    make_hero_image(marrakesh, "https://example.invalid/marrakesh-hero.jpg")

    grid = destinations_grid(Client().get("/ar/").content.decode())

    assert "https://example.invalid/riyadh-hero.jpg" in grid
    assert "https://example.invalid/marrakesh-hero.jpg" in grid
    # The stock photographs are only a fallback and must step aside.
    assert "images.unsplash.com" not in grid


def test_home_page_falls_back_to_stock_for_a_city_without_a_nominated_photo() -> None:
    make_hero_image(make_property(103), "https://example.invalid/riyadh-hero.jpg")

    grid = destinations_grid(Client().get("/ar/").content.decode())

    assert "https://example.invalid/riyadh-hero.jpg" in grid
    # Marrakesh has nothing nominated, so its card keeps the stock image
    # rather than rendering an empty src.
    assert "images.unsplash.com" in grid


@pytest.mark.parametrize(
    ("is_city_hero", "is_visible", "property_visible"),
    [
        (False, True, True),  # not nominated
        (True, False, True),  # nominated but hidden
        (True, True, False),  # nominated on an unpublished property
    ],
)
def test_only_a_public_nominated_photo_reaches_the_home_page(
    is_city_hero: bool,
    is_visible: bool,
    property_visible: bool,
) -> None:
    property_obj = make_property(104, visible=property_visible)
    make_hero_image(
        property_obj,
        "https://example.invalid/should-not-appear.jpg",
        is_city_hero=is_city_hero,
        is_visible=is_visible,
    )

    grid = destinations_grid(Client().get("/ar/").content.decode())

    assert "https://example.invalid/should-not-appear.jpg" not in grid
    assert RIYADH_STOCK_PHOTO in grid


def test_a_city_with_two_nominated_photos_resolves_to_one_deterministically() -> None:
    plain = make_property(105)
    featured = make_property(106)
    featured.is_featured = True
    featured.save(update_fields=["is_featured"])
    make_hero_image(plain, "https://example.invalid/plain.jpg")
    make_hero_image(featured, "https://example.invalid/featured.jpg")

    grid = destinations_grid(Client().get("/ar/").content.decode())

    # The featured property wins, matching how the listing orders properties.
    assert "https://example.invalid/featured.jpg" in grid
    assert "https://example.invalid/plain.jpg" not in grid
