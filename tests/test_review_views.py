import pytest
from django.test import Client

from apps.properties.models import Property

pytestmark = pytest.mark.django_db


def review_property(*, listing_id: int, slug: str) -> Property:
    return Property.objects.create(
        hostaway_listing_id=listing_id,
        slug=slug,
        hostaway_name=f"Stay {listing_id}",
        name_ar=f"إقامة {listing_id}",
        name_en=f"Stay {listing_id}",
        name_fr=f"Séjour {listing_id}",
        city="Riyadh",
        country_code="SA",
        is_visible=True,
        hostaway_special_status="",
    )


def test_public_review_page_uses_the_trustindex_widget_only() -> None:
    response = Client().get("/ar/reviews/")

    assert response.status_code == 200
    content = response.content.decode()
    assert "data-trustindex-general-reviews" in content
    assert "cdn.trustindex.io/loader.js?cf56bfb80db642820b86b5cdc90" in content
    assert "hostaway" not in content.casefold()
    csp = response.headers["Content-Security-Policy"]
    assert "https://cdn.trustindex.io" in csp
    assert "https://lh3.googleusercontent.com" in csp


def test_public_review_page_can_narrow_reviews_to_one_property() -> None:
    reviewed_property = review_property(listing_id=315814, slug="reviewed-stay")
    unreviewed_property = review_property(listing_id=999999, slug="unreviewed-stay")

    response = Client().get("/ar/reviews/?property=reviewed-stay")

    assert response.status_code == 200
    content = response.content.decode()
    assert 'name="property"' in content
    assert f'value="{reviewed_property.slug}" selected' in content
    assert f'value="{unreviewed_property.slug}"' in content
    assert "cdn.trustindex.io/loader.js?18981608195554449116091f051" in content
    assert "Google أو Booking.com أو Airbnb" in content

    response = Client().get(f"/ar/reviews/?property={unreviewed_property.slug}")

    assert response.status_code == 200
    content = response.content.decode()
    assert f'value="{unreviewed_property.slug}" selected' in content
    assert "يجري إعداد المراجعات الموثقة" in content
    assert "cdn.trustindex.io/loader.js" not in content
