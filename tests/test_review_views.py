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
        hostaway_listing_map_id=property_obj.hostaway_listing_id,
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
        hostaway_listing_id=7100,
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


def test_long_review_has_accessible_expand_control() -> None:
    property_obj = Property.objects.create(
        hostaway_listing_id=7200,
        slug="long-review-property",
        name_ar="وحدة الاختبار",
        name_en="Test Property",
        city_ar="الرياض",
        city_en="Riyadh",
        country_code="SA",
    )
    review = make_review(property_obj, 9200, visible=True)
    review.public_review = "تجربة إقامة رائعة ومريحة. " * 20
    review.save(update_fields=["public_review"])

    content = Client().get("/reviews/").content.decode()

    assert 'data-review-copy class="is-collapsible"' in content
    assert "data-review-toggle" in content
    assert 'aria-controls="review-copy-' in content


def test_review_collection_supports_progressive_reveal() -> None:
    property_obj = Property.objects.create(
        hostaway_listing_id=7300,
        slug="review-collection-property",
        name_ar="وحدة الاختبار",
        name_en="Test Property",
        city_ar="الرياض",
        city_en="Riyadh",
        country_code="SA",
    )
    for review_id in range(9300, 9307):
        make_review(property_obj, review_id, visible=True)

    content = Client().get("/reviews/").content.decode()

    assert "data-review-collection" in content
    assert 'data-initial-count="6"' in content
    assert 'data-mobile-initial-count="3"' in content
    assert 'data-batch-size="3"' in content
    assert "data-review-more" in content
