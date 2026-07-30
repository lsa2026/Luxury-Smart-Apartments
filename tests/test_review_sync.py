from copy import deepcopy
from typing import Any

import pytest

from apps.integrations.hostaway.services import sync_reviews
from apps.properties.models import Property
from apps.reviews.models import Review

pytestmark = pytest.mark.django_db


class FakeHostawayClient:
    def __init__(self, pages: list[tuple[list[dict[str, Any]], int | None]]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    def get_reviews(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int | None]:
        self.calls.append(kwargs)
        return self.pages.pop(0)


@pytest.fixture
def property_obj() -> Property:
    return Property.objects.create(
        hostaway_listing_id=6001,
        hostaway_listing_map_id=7001,
        slug="riyadh-suite",
        name_ar="جناح الرياض",
        name_en="Riyadh Suite",
        city_ar="الرياض",
        city_en="Riyadh",
        country_code="SA",
    )


@pytest.fixture
def hostaway_review() -> dict[str, Any]:
    return {
        "id": 9001,
        "listingMapId": 7001,
        "reservationId": 8001,
        "externalReviewId": "ext-1",
        "channelId": 2000,
        "type": "guest-to-host",
        "status": "published",
        "guestName": "سارة محمد",
        "rating": "9.5",
        "publicReview": "إقامة ممتازة وهادئة.",
        "privateFeedback": "Should never be stored.",
        "revieweeResponse": "شكرًا لك.",
        "arrivalDate": "2026-05-01",
        "departureDate": "2026-05-04",
        "updatedOn": "2026-05-05T10:30:00Z",
    }


def test_creates_new_review_from_hostaway(
    property_obj: Property,
    hostaway_review: dict[str, Any],
) -> None:
    report = sync_reviews(client=FakeHostawayClient([([hostaway_review], 1)]))

    review = Review.objects.get()
    assert report.created == 1
    assert review.property == property_obj
    assert review.hostaway_review_id == 9001
    assert str(review.rating) == "9.5"
    assert review.public_review == "إقامة ممتازة وهادئة."
    assert report.match_strategies == {
        "listing_map_id": 1,
        "listing_id_fallback": 0,
        "unmatched": 0,
    }


def test_updates_existing_review_without_duplication(
    property_obj: Property,
    hostaway_review: dict[str, Any],
) -> None:
    sync_reviews(client=FakeHostawayClient([([hostaway_review], 1)]))
    updated_payload = deepcopy(hostaway_review)
    updated_payload["publicReview"] = "نص منشور محدث."

    report = sync_reviews(client=FakeHostawayClient([([updated_payload], 1)]))

    assert report.updated == 1
    assert Review.objects.count() == 1
    assert Review.objects.get().public_review == "نص منشور محدث."


def test_private_feedback_is_never_stored(
    property_obj: Property,
    hostaway_review: dict[str, Any],
) -> None:
    sync_reviews(client=FakeHostawayClient([([hostaway_review], 1)]))

    review = Review.objects.get()
    assert not hasattr(review, "private_feedback")
    assert "Should never be stored." not in review.public_review
    assert "Should never be stored." not in review.reviewee_response


@pytest.mark.parametrize(
    ("changes", "expected_skipped"),
    [
        ({"type": "host-to-guest"}, 1),
        ({"status": "awaiting"}, 1),
        ({"publicReview": ""}, 1),
        ({"rating": None}, 1),
    ],
)
def test_ineligible_reviews_are_ignored(
    property_obj: Property,
    hostaway_review: dict[str, Any],
    changes: dict[str, Any],
    expected_skipped: int,
) -> None:
    hostaway_review.update(changes)
    report = sync_reviews(client=FakeHostawayClient([([hostaway_review], 1)]))

    assert report.skipped == expected_skipped
    assert Review.objects.count() == 0


def test_admin_visibility_choice_survives_sync(
    property_obj: Property,
    hostaway_review: dict[str, Any],
) -> None:
    sync_reviews(client=FakeHostawayClient([([hostaway_review], 1)]))
    Review.objects.update(is_visible=False)

    sync_reviews(client=FakeHostawayClient([([hostaway_review], 1)]))

    assert Review.objects.get().is_visible is False


def test_pagination_fetches_every_page(
    property_obj: Property,
    hostaway_review: dict[str, Any],
) -> None:
    second = deepcopy(hostaway_review)
    second["id"] = 9002
    third = deepcopy(hostaway_review)
    third["id"] = 9003
    client = FakeHostawayClient(
        [
            ([hostaway_review, second], 3),
            ([third], 3),
        ]
    )

    report = sync_reviews(client=client, page_size=2)

    assert report.fetched == 3
    assert report.created == 3
    assert Review.objects.count() == 3
    assert [call["offset"] for call in client.calls] == [0, 2]
    assert all(call["review_type"] == "guest-to-host" for call in client.calls)
    assert all(call["statuses"] == ["published"] for call in client.calls)


def test_dry_run_does_not_write(
    property_obj: Property,
    hostaway_review: dict[str, Any],
) -> None:
    report = sync_reviews(
        client=FakeHostawayClient([([hostaway_review], 1)]),
        dry_run=True,
    )

    assert report.created == 1
    assert Review.objects.count() == 0


def test_review_matches_property_by_listing_id_fallback(
    property_obj: Property,
    hostaway_review: dict[str, Any],
) -> None:
    property_obj.hostaway_listing_map_id = None
    property_obj.save(update_fields=["hostaway_listing_map_id"])
    hostaway_review["listingMapId"] = property_obj.hostaway_listing_id

    report = sync_reviews(client=FakeHostawayClient([([hostaway_review], 1)]))

    assert Review.objects.get().property == property_obj
    assert report.match_strategies["listing_id_fallback"] == 1
    assert report.match_strategies["listing_map_id"] == 0


def test_unmatched_review_is_saved_without_property(
    property_obj: Property,
    hostaway_review: dict[str, Any],
) -> None:
    hostaway_review["listingMapId"] = 999999

    report = sync_reviews(client=FakeHostawayClient([([hostaway_review], 1)]))

    assert Review.objects.get().property is None
    assert report.match_strategies["unmatched"] == 1
    assert Property.objects.count() == 1


def test_listing_map_id_can_differ_from_listing_id(
    property_obj: Property,
    hostaway_review: dict[str, Any],
) -> None:
    assert property_obj.hostaway_listing_map_id != property_obj.hostaway_listing_id

    report = sync_reviews(client=FakeHostawayClient([([hostaway_review], 1)]))

    assert Review.objects.get().property == property_obj
    assert report.match_strategies["listing_map_id"] == 1


def test_listing_map_id_match_wins_over_listing_id_fallback(
    hostaway_review: dict[str, Any],
) -> None:
    fallback_candidate = Property.objects.create(
        hostaway_listing_id=7001,
        slug="fallback-candidate",
    )
    map_candidate = Property.objects.create(
        hostaway_listing_id=8001,
        hostaway_listing_map_id=7001,
        slug="map-candidate",
    )

    report = sync_reviews(client=FakeHostawayClient([([hostaway_review], 1)]))

    review = Review.objects.get()
    assert review.property == map_candidate
    assert review.property != fallback_candidate
    assert report.match_strategies["listing_map_id"] == 1
    assert report.match_strategies["listing_id_fallback"] == 0
