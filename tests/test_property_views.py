from decimal import Decimal

import pytest
from django.test import Client

from apps.properties.models import Property, PropertyImage

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

    response = Client().get("/properties/")
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


def test_enabled_public_location_renders_an_interactive_map_and_geo_schema() -> None:
    property_obj = make_property(11)
    property_obj.public_location_enabled = True
    property_obj.public_location_latitude = Decimal("24.713552")
    property_obj.public_location_longitude = Decimal("46.675296")
    property_obj.save(
        update_fields=[
            "public_location_enabled",
            "public_location_latitude",
            "public_location_longitude",
        ]
    )

    response = Client().get(property_obj.get_absolute_url())
    content = response.content.decode()

    assert response.status_code == 200
    assert "data-property-map" in content
    assert 'data-latitude="24.713552"' in content
    assert 'data-longitude="46.675296"' in content
    assert "vendor/leaflet/leaflet.js" in content
    assert "openstreetmap.org" in content
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

    first_page = Client().get("/properties/")
    second_page = Client().get("/properties/?page=2")

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
        response = Client().get("/properties/")
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

    grid = destinations_grid(Client().get("/").content.decode())

    assert "https://example.invalid/riyadh-hero.jpg" in grid
    assert "https://example.invalid/marrakesh-hero.jpg" in grid
    # The stock photographs are only a fallback and must step aside.
    assert "images.unsplash.com" not in grid


def test_home_page_falls_back_to_stock_for_a_city_without_a_nominated_photo() -> None:
    make_hero_image(make_property(103), "https://example.invalid/riyadh-hero.jpg")

    grid = destinations_grid(Client().get("/").content.decode())

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

    grid = destinations_grid(Client().get("/").content.decode())

    assert "https://example.invalid/should-not-appear.jpg" not in grid
    assert RIYADH_STOCK_PHOTO in grid


def test_a_city_with_two_nominated_photos_resolves_to_one_deterministically() -> None:
    plain = make_property(105)
    featured = make_property(106)
    featured.is_featured = True
    featured.save(update_fields=["is_featured"])
    make_hero_image(plain, "https://example.invalid/plain.jpg")
    make_hero_image(featured, "https://example.invalid/featured.jpg")

    grid = destinations_grid(Client().get("/").content.decode())

    # The featured property wins, matching how the listing orders properties.
    assert "https://example.invalid/featured.jpg" in grid
    assert "https://example.invalid/plain.jpg" not in grid
