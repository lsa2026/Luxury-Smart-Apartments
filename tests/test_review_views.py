import pytest
from django.test import Client

from apps.properties.models import Property

pytestmark = pytest.mark.django_db


def make_property(listing_id: int, *, widget_id: str = "") -> Property:
    return Property.objects.create(
        hostaway_listing_id=listing_id,
        slug=f"review-property-{listing_id}",
        name_ar="وحدة الاختبار",
        name_en="Test Property",
        city_ar="الرياض",
        city_en="Riyadh",
        country_code="SA",
        trustindex_widget_id=widget_id,
    )


def test_review_explorer_never_renders_legacy_hostaway_review_copy() -> None:
    property_obj = make_property(7100)

    response = Client().get("/ar/reviews/")

    assert response.status_code == 200
    content = response.content.decode()
    assert property_obj.slug in content
    assert "cdn.trustindex.io/loader.js" in content
    assert "Review text" not in content


def test_review_explorer_accepts_a_property_filter() -> None:
    selected = make_property(7200)
    make_property(7201)

    response = Client().get("/ar/reviews/", {"property": selected.slug})

    assert response.status_code == 200
    assert f'value="{selected.slug}" selected' in response.content.decode()


def test_property_review_page_returns_not_found_without_approved_widget() -> None:
    property_obj = make_property(7300)

    response = Client().get(f"/ar/properties/{property_obj.slug}/reviews/")

    assert response.status_code == 404
