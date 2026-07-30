import pytest
from django.test import Client

from apps.properties.models import Property, PropertyImage

pytestmark = pytest.mark.django_db


def make_property(listing_id: int, *, visible: bool = True, active: bool = True) -> Property:
    return Property.objects.create(
        hostaway_listing_map_id=listing_id,
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
