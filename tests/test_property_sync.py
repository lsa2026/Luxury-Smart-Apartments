from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction

from apps.integrations.hostaway.property_services import (
    PropertySyncReport,
    _fetch_listings,
    sync_properties,
)
from apps.integrations.models import IntegrationSyncRun
from apps.properties.models import Amenity, Property, PropertyAmenity, PropertyImage

pytestmark = pytest.mark.django_db


class FakePropertyClient:
    def __init__(
        self,
        *,
        detail: dict[str, Any] | None = None,
        pages: list[tuple[list[dict[str, Any]], int | None]] | None = None,
        amenities: list[dict[str, Any]] | None = None,
    ) -> None:
        self.detail = detail
        self.pages = list(pages or [])
        self.amenities = amenities or []
        self.list_calls: list[dict[str, Any]] = []

    def get_listing(
        self,
        listing_id: int,
        *,
        include_resources: bool = True,
    ) -> dict[str, Any]:
        assert self.detail is not None
        return deepcopy(self.detail)

    def get_listings(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int | None]:
        self.list_calls.append(kwargs)
        page, count = self.pages.pop(0)
        return deepcopy(page), count

    def get_amenities(self) -> list[dict[str, Any]]:
        return deepcopy(self.amenities)


@pytest.fixture
def listing_payload() -> dict[str, Any]:
    return {
        "id": 40160,
        "name": "Riyadh Smart Suite",
        "description": "Operational description",
        "internalListingName": "RS-01",
        "propertyTypeId": 1,
        "roomType": "entire_home",
        "personCapacity": 4,
        "bedroomsNumber": 2,
        "bedsNumber": 3,
        "bathroomsNumber": "1.5",
        "address": "Full private address",
        "publicAddress": "Riyadh, Saudi Arabia",
        "city": "Riyadh",
        "state": "Riyadh",
        "country": "Saudi Arabia",
        "countryCode": "SA",
        "zipcode": "12345",
        "lat": "24.713600",
        "lng": "46.675300",
        "currencyCode": "SAR",
        "averageReviewRating": "9.2",
        "specialStatus": None,
        "listingImages": [
            {
                "id": 877,
                "url": "https://hostaway-platform.s3.us-west-2.amazonaws.com/listing/877.jpg",
                "caption": "Living room",
                "sortOrder": 1,
            }
        ],
        "listingAmenities": [{"id": 3449, "amenityId": 2}],
    }


def sync_one(
    payload: dict[str, Any],
    *,
    amenities: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> object:
    return sync_properties(
        listing_id=payload["id"],
        client=FakePropertyClient(detail=payload, amenities=amenities),
        dry_run=dry_run,
    )


def test_creates_new_property_and_embedded_resources(
    listing_payload: dict[str, Any],
) -> None:
    report = sync_one(
        listing_payload,
        amenities=[{"id": 2, "name": "Air conditioning"}],
    )

    property_obj = Property.objects.get()
    assert report.properties_created == 1
    assert property_obj.hostaway_listing_id == 40160
    assert property_obj.hostaway_listing_map_id is None
    assert property_obj.hostaway_name == "Riyadh Smart Suite"
    assert property_obj.name_en == "Riyadh Smart Suite"
    assert property_obj.name_ar == ""
    assert property_obj.slug == "riyadh-smart-suite"
    assert property_obj.hostaway_property_type_id == 1
    assert property_obj.hostaway_special_status == ""
    assert property_obj.hostaway_is_active is True
    assert property_obj.person_capacity == 4
    assert PropertyImage.objects.count() == 1
    assert PropertyAmenity.objects.count() == 1
    assert IntegrationSyncRun.objects.get().status == IntegrationSyncRun.Status.SUCCEEDED


def test_updates_operational_data_but_preserves_all_local_content(
    listing_payload: dict[str, Any],
) -> None:
    sync_one(listing_payload)
    property_obj = Property.objects.get()
    property_obj.name_ar = "جناح الرياض الخاص"
    property_obj.name_en = "Custom English title"
    property_obj.slug = "custom-slug"
    property_obj.description_ar = "وصف محلي"
    property_obj.seo_title_ar = "عنوان SEO"
    property_obj.is_visible = False
    property_obj.is_featured = True
    property_obj.sort_order = 17
    property_obj.save()

    changed = deepcopy(listing_payload)
    changed["name"] = "Changed Hostaway name"
    changed["personCapacity"] = 6
    changed["address"] = "Changed operational address"
    report = sync_one(changed)

    property_obj.refresh_from_db()
    assert report.properties_updated == 1
    assert property_obj.hostaway_name == "Changed Hostaway name"
    assert property_obj.person_capacity == 6
    assert property_obj.address == "Changed operational address"
    assert property_obj.name_ar == "جناح الرياض الخاص"
    assert property_obj.name_en == "Custom English title"
    assert property_obj.slug == "custom-slug"
    assert property_obj.description_ar == "وصف محلي"
    assert property_obj.seo_title_ar == "عنوان SEO"
    assert property_obj.is_visible is False
    assert property_obj.is_featured is True
    assert property_obj.sort_order == 17


def test_hostaway_image_is_idempotent_and_hidden_choice_survives(
    listing_payload: dict[str, Any],
) -> None:
    sync_one(listing_payload)
    image = PropertyImage.objects.get()
    image.is_visible = False
    image.title_ar = "عنوان محلي"
    image.save()

    report = sync_one(listing_payload)

    image.refresh_from_db()
    assert PropertyImage.objects.count() == 1
    assert report.images_updated == 1
    assert image.is_visible is False
    assert image.title_ar == "عنوان محلي"


def test_url_fallback_key_prevents_duplicate_without_image_id(
    listing_payload: dict[str, Any],
) -> None:
    listing_payload["listingImages"][0].pop("id")
    sync_one(listing_payload)
    sync_one(listing_payload)

    assert PropertyImage.objects.count() == 1
    assert PropertyImage.objects.get().sync_key.startswith("url:")


def test_missing_hostaway_image_is_deactivated_but_local_image_is_not(
    listing_payload: dict[str, Any],
) -> None:
    sync_one(listing_payload)
    property_obj = Property.objects.get()
    local_image = PropertyImage.objects.create(
        property=property_obj,
        image="properties/1/local.jpg",
        source=PropertyImage.Source.LOCAL,
    )
    changed = deepcopy(listing_payload)
    changed["listingImages"] = []

    report = sync_one(changed)

    hostaway_image = PropertyImage.objects.get(source=PropertyImage.Source.HOSTAWAY)
    local_image.refresh_from_db()
    assert report.images_deactivated == 1
    assert hostaway_image.is_active_at_source is False
    assert local_image.is_active_at_source is True


def test_invalid_child_image_does_not_abort_property(
    listing_payload: dict[str, Any],
) -> None:
    listing_payload["listingImages"].append({"id": 999, "url": "http://unsafe.example/image.jpg"})

    report = sync_one(listing_payload)

    assert report.properties_created == 1
    assert report.skipped == 1
    assert PropertyImage.objects.count() == 1


def test_only_one_visible_cover_per_property(
    listing_payload: dict[str, Any],
) -> None:
    sync_one(listing_payload)
    property_obj = Property.objects.get()
    first = PropertyImage.objects.get()
    first.is_cover = True
    first.save()

    with pytest.raises(IntegrityError), transaction.atomic():
        PropertyImage.objects.create(
            property=property_obj,
            hostaway_image_id=999,
            hostaway_url="https://hostaway-platform.s3.us-west-2.amazonaws.com/999.jpg",
            sync_key="id:999",
            source=PropertyImage.Source.HOSTAWAY,
            is_cover=True,
        )


def test_amenities_are_idempotent_and_preserve_arabic_name(
    listing_payload: dict[str, Any],
) -> None:
    definitions = [{"id": 2, "name": "Air conditioning"}]
    sync_one(listing_payload, amenities=definitions)
    amenity = Amenity.objects.get()
    amenity.name_ar = "تكييف"
    amenity.save()

    changed_definitions = [{"id": 2, "name": "Updated source name"}]
    sync_one(listing_payload, amenities=changed_definitions)

    amenity.refresh_from_db()
    assert Amenity.objects.count() == 1
    assert PropertyAmenity.objects.count() == 1
    assert amenity.name == "Updated source name"
    assert amenity.name_ar == "تكييف"


def test_property_list_pagination_is_followed(
    listing_payload: dict[str, Any],
) -> None:
    second = deepcopy(listing_payload)
    second["id"] = 40161
    second["name"] = "Second"
    client = FakePropertyClient(
        pages=[
            ([listing_payload], 2),
            ([second], 2),
        ]
    )

    report = sync_properties(
        client=client,
        include_images=False,
        include_amenities=False,
    )

    assert report.fetched == 2
    assert report.properties_created == 2
    assert [call["offset"] for call in client.list_calls] == [0, 1]


def test_dry_run_makes_no_database_changes(
    listing_payload: dict[str, Any],
) -> None:
    report = sync_one(listing_payload, dry_run=True)

    assert report.properties_created == 1
    assert Property.objects.count() == 0
    assert PropertyImage.objects.count() == 0
    assert Amenity.objects.count() == 0
    assert IntegrationSyncRun.objects.count() == 0


def test_one_bad_listing_does_not_stop_the_remaining_list(
    listing_payload: dict[str, Any],
) -> None:
    invalid = deepcopy(listing_payload)
    invalid["id"] = None
    client = FakePropertyClient(
        pages=[
            ([invalid, listing_payload], 2),
        ]
    )

    report = sync_properties(
        client=client,
        include_images=False,
        include_amenities=False,
    )

    assert report.properties_failed == 1
    assert report.properties_created == 1
    assert Property.objects.count() == 1


def test_archived_special_status_is_stored_and_derives_inactive(
    listing_payload: dict[str, Any],
) -> None:
    listing_payload["specialStatus"] = "archived"

    sync_one(listing_payload)

    property_obj = Property.objects.get()
    assert property_obj.hostaway_special_status == "archived"
    assert property_obj.hostaway_is_active is False


def test_unknown_special_status_is_stored_logged_and_remains_active(
    listing_payload: dict[str, Any],
) -> None:
    listing_payload["specialStatus"] = "future-paused-state"

    with patch("apps.integrations.hostaway.listing_validators.logger.warning") as warning:
        report = sync_one(listing_payload)

    property_obj = Property.objects.get()
    assert report.properties_created == 1
    assert property_obj.hostaway_special_status == "future-paused-state"
    assert property_obj.hostaway_is_active is True
    warning.assert_called_once()


def test_listing_id_is_primary_and_map_id_remains_empty_when_absent(
    listing_payload: dict[str, Any],
) -> None:
    assert "listingMapId" not in listing_payload

    sync_one(listing_payload)

    property_obj = Property.objects.get()
    assert property_obj.hostaway_listing_id == listing_payload["id"]
    assert property_obj.hostaway_listing_map_id is None


def test_listing_map_id_is_stored_only_when_present(
    listing_payload: dict[str, Any],
) -> None:
    listing_payload["listingMapId"] = 990040160

    sync_one(listing_payload)

    property_obj = Property.objects.get()
    assert property_obj.hostaway_listing_id == listing_payload["id"]
    assert property_obj.hostaway_listing_map_id == listing_payload["listingMapId"]


def test_pagination_stops_when_count_is_smaller_than_limit() -> None:
    client = FakePropertyClient(pages=[([{"id": 1}], 1)])
    report = PropertySyncReport()

    records = _fetch_listings(
        client,
        report,
        listing_id=None,
        include_resources=False,
        limit=None,
    )

    assert records == [{"id": 1}]
    assert report.fetched == 1
    assert [call["offset"] for call in client.list_calls] == [0]


def test_pagination_stops_after_an_empty_last_page() -> None:
    first_page = [{"id": listing_id} for listing_id in range(1, 101)]
    client = FakePropertyClient(pages=[(first_page, None), ([], None)])
    report = PropertySyncReport()

    records = _fetch_listings(
        client,
        report,
        listing_id=None,
        include_resources=False,
        limit=None,
    )

    assert len(records) == 100
    assert report.fetched == 100
    assert [call["offset"] for call in client.list_calls] == [0, 100]


def test_latest_activity_is_used_when_updated_on_is_absent(
    listing_payload: dict[str, Any],
) -> None:
    listing_payload["latestActivityOn"] = "2026-07-30T01:02:03Z"

    sync_one(listing_payload)

    source_updated_at = Property.objects.get().source_updated_at
    assert source_updated_at is not None
    assert source_updated_at.isoformat() == "2026-07-30T01:02:03+00:00"


def test_local_upload_rejects_non_image(
    listing_payload: dict[str, Any],
) -> None:
    sync_one(listing_payload)
    property_obj = Property.objects.get()
    image = PropertyImage(
        property=property_obj,
        source=PropertyImage.Source.LOCAL,
        image=SimpleUploadedFile(
            "not-an-image.jpg",
            b"this is not image content",
            content_type="image/jpeg",
        ),
    )

    with pytest.raises(ValidationError, match="valid image"):
        image.full_clean()
