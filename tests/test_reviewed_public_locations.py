import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from apps.properties.management.commands.publish_hostaway_property_locations import (
    _load_business_profiles,
)
from tests.test_property_airport_directions import make_property

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("listing_id", "imported_lat", "imported_lon", "reviewed_lat", "reviewed_lon", "place_id"),
    [
        (315816, "24.794822", "46.612351", "24.793941", "46.610402", "ChIJl-vqdXvjLj4RCIYB5DldC5k"),
        (325961, "24.836076", "46.748926", "24.837618", "46.748429", "ChIJsy-YIQD_Lj4RIUmilSxYynI"),
        (343666, "24.837438", "46.748188", "24.837417", "46.748219", "ChIJVaDhVZD_Lj4RES0Nej_3-XI"),
        (511786, "31.645607", "-8.017481", "31.647129", "-8.015223", "ChIJ54Web3jvrw0RENTa4RVQ0uE"),
    ],
)
def test_publish_reviewed_coordinates_without_changing_imported_data(
    listing_id, imported_lat, imported_lon, reviewed_lat, reviewed_lon, place_id
):
    reviewed, darat = make_property(listing_id), make_property(315815)
    for property_obj in (reviewed, darat):
        property_obj.latitude = Decimal(imported_lat)
        property_obj.longitude = Decimal(imported_lon)
        property_obj.public_address = "Existing public address"
        property_obj.public_location_latitude = property_obj.latitude
        property_obj.public_location_longitude = property_obj.longitude
        property_obj.save()
    profiles = _load_business_profiles()
    with patch(
        "apps.properties.management.commands.publish_hostaway_property_locations."
        "_load_business_profiles",
        return_value={
            str(p.hostaway_listing_id): profiles[str(p.hostaway_listing_id)]
            for p in (reviewed, darat)
        },
    ):
        call_command("publish_hostaway_property_locations", dry_run=True, verbosity=0)
        reviewed.refresh_from_db()
        assert reviewed.public_location_latitude == Decimal(imported_lat)
        call_command("publish_hostaway_property_locations", verbosity=0)
        call_command("publish_hostaway_property_locations", verbosity=0)
    reviewed.refresh_from_db()
    darat.refresh_from_db()
    assert reviewed.public_location_latitude == Decimal(reviewed_lat)
    assert reviewed.public_location_longitude == Decimal(reviewed_lon)
    for property_obj in (reviewed, darat):
        assert property_obj.latitude == Decimal(imported_lat)
        assert property_obj.longitude == Decimal(imported_lon)
        assert property_obj.public_address == "Existing public address"
        assert property_obj.currency_code == "SAR"
    assert darat.public_location_latitude == darat.latitude
    assert darat.public_location_longitude == darat.longitude
    html = Client().get(f"/ar/properties/{reviewed.slug}/").content.decode()
    assert f'data-latitude="{reviewed_lat}"' in html
    assert f'data-longitude="{reviewed_lon}"' in html
    assert place_id in html


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (None, None),
        ("NaN", "46"),
        ("24", None),
        (91, "46"),
        ("24", "181"),
        ("24.7939406", "46.610402"),
        ("Infinity", "46"),
    ],
)
def test_invalid_public_map_correction_is_rejected_before_writes(tmp_path, latitude, longitude):
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "315816": {
                    "google_maps_cid": "11028010615766615560",
                    "public_location_latitude": latitude,
                    "public_location_longitude": longitude,
                }
            }
        ),
        encoding="utf-8",
    )
    with (
        patch(
            "apps.properties.management.commands.publish_hostaway_property_locations."
            "BUSINESS_PROFILE_PATH",
            path,
        ),
        pytest.raises(CommandError, match="coordinate pair"),
    ):
        _load_business_profiles()


@pytest.mark.parametrize("listing_id", [315816, 325961, 343666, 511786])
def test_release_fixture_changes_only_public_reviewed_coordinates(listing_id):
    path = Path(__file__).parents[1] / "apps/properties/data/release_properties.json"
    properties = [
        entry["fields"]
        for entry in json.loads(path.read_text(encoding="utf-8"))
        if entry["model"] == "properties.property"
    ]
    property_data = next(p for p in properties if p["hostaway_listing_id"] == listing_id)
    reviewed = _load_business_profiles()[str(listing_id)]
    assert property_data["public_location_latitude"] == reviewed["public_location_latitude"]
    assert property_data["public_location_longitude"] == reviewed["public_location_longitude"]
    assert (property_data["latitude"], property_data["longitude"]) == {
        315816: ("24.794822", "46.612351"),
        325961: ("24.836076", "46.748926"),
        343666: ("24.837438", "46.748188"),
        511786: ("31.645607", "-8.017481"),
    }[listing_id]
