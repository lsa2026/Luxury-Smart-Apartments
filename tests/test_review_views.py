import pytest
from django.test import Client
from django.utils import timezone

from apps.properties.models import Property
from apps.reviews.models import Review

pytestmark = pytest.mark.django_db


def make_review(property_obj: Property, review_id: int, *, visible: bool) -> Review:
    return Review.objects.create(
        hostaway_review_id=review_id,
        property=property_obj,
        hostaway_listing_map_id=property_obj.hostaway_listing_map_id,
        review_type=Review.Type.GUEST_TO_HOST,
        status=Review.Status.PUBLISHED,
        guest_name="عبدالله الكامل",
        rating="9.0",
        public_review=f"Review text {review_id}",
        is_visible=visible,
        synced_at=timezone.now(),
    )


def test_hidden_reviews_do_not_appear_in_public_view() -> None:
    property_obj = Property.objects.create(
        hostaway_listing_map_id=7100,
        slug="test-property",
        name_ar="وحدة الاختبار",
        name_en="Test Property",
        city_ar="الرياض",
        city_en="Riyadh",
        country_code="SA",
    )
    make_review(property_obj, 9100, visible=True)
    make_review(property_obj, 9101, visible=False)

    response = Client().get("/reviews/")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Review text 9100" in content
    assert "Review text 9101" not in content
    assert "عبدالله الكامل" not in content
    assert "عبدالله" in content
