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


def test_publish_uses_e12_reviewed_coordinates_without_changing_imported_data():
    e12, darat = make_property(315816), make_property(315815)
    for property_obj in (e12, darat):
        property_obj.latitude = Decimal("24.794822")
        property_obj.longitude = Decimal("46.612351")
        property_obj.public_address = "Existing public address"
        property_obj.public_location_latitude = property_obj.latitude
        property_obj.public_location_longitude = property_obj.longitude
        property_obj.save()
    profiles = _load_business_profiles()
    with patch(
        "apps.properties.management.commands.publish_hostaway_property_locations."
        "_load_business_profiles",
        return_value={
            str(p.hostaway_listing_id): profiles[str(p.hostaway_listing_id)] for p in (e12, darat)
        },
    ):
        call_command("publish_hostaway_property_locations", dry_run=True, verbosity=0)
        e12.refresh_from_db()
        assert e12.public_location_latitude == Decimal("24.794822")
        call_command("publish_hostaway_property_locations", verbosity=0)
        call_command("publish_hostaway_property_locations", verbosity=0)
    e12.refresh_from_db()
    darat.refresh_from_db()
    assert e12.public_location_latitude == Decimal("24.793941")
    assert e12.public_location_longitude == Decimal("46.610402")
    for property_obj in (e12, darat):
        assert property_obj.latitude == Decimal("24.794822")
        assert property_obj.longitude == Decimal("46.612351")
        assert property_obj.public_address == "Existing public address"
        assert property_obj.currency_code == "SAR"
    assert darat.public_location_latitude == darat.latitude
    assert darat.public_location_longitude == darat.longitude
    html = Client().get(f"/ar/properties/{e12.slug}/").content.decode()
    assert 'data-latitude="24.793941"' in html
    assert 'data-longitude="46.610402"' in html
    assert "ChIJl-vqdXvjLj4RCIYB5DldC5k" in html


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


def test_release_fixture_changes_only_public_e12_coordinates():
    path = Path(__file__).parents[1] / "apps/properties/data/release_properties.json"
    properties = [
        entry["fields"]
        for entry in json.loads(path.read_text(encoding="utf-8"))
        if entry["model"] == "properties.property"
    ]
    e12 = next(p for p in properties if p["hostaway_listing_id"] == 315816)
    assert e12["public_location_latitude"] == "24.793941"
    assert e12["public_location_longitude"] == "46.610402"
    assert e12["latitude"] == "24.794822"
    assert e12["longitude"] == "46.612351"
