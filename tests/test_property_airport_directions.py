from decimal import Decimal
from html import unescape
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client

from apps.properties.directions import airport_directions
from apps.properties.models import Property

pytestmark = pytest.mark.django_db

AIRPORT_URL = (
    "https://www.google.com/maps/dir/?api=1&origin=24.959443,46.7010829"
    "&destination=Luxury%20Smart%20Apartment%20Safa%2041%20B1"
    "&destination_place_id=ChIJ_cSOVgAdLz4RXGVt2wTBIUU&travelmode=driving"
)


def make_property(listing_id: int = 315814) -> Property:
    return Property.objects.create(
        hostaway_listing_id=listing_id,
        slug=f"property-{listing_id}",
        hostaway_name="Safa 41 B1",
        name_ar="شقة عرقة الذكية",
        name_en="Safa 41 B1",
        city="Riyadh",
        currency_code="SAR",
        is_visible=True,
        public_location_enabled=True,
        public_location_latitude=Decimal("24.680000"),
        public_location_longitude=Decimal("46.580000"),
        google_maps_cid="4981474889453888860",
    )


def test_directions_use_exact_verified_origin_and_destination() -> None:
    property_obj = make_property()
    links = airport_directions(property_obj)
    assert links is not None
    assert links["airport_url"] == AIRPORT_URL
    other_url = urlparse(links["other_origin_url"])
    assert other_url.netloc == "www.google.com"
    assert other_url.path == "/maps/dir/"
    assert parse_qs(other_url.query) == {
        "api": ["1"],
        "destination": ["Luxury Smart Apartment Safa 41 B1"],
        "destination_place_id": ["ChIJ_cSOVgAdLz4RXGVt2wTBIUU"],
        "travelmode": ["driving"],
    }


@pytest.mark.parametrize(
    ("language", "estimate", "airport_label", "other_label"),
    [
        (
            "ar",
            "نحو 45 كم · حوالي 35 دقيقة بالسيارة",
            "الاتجاهات من المطار",
            "الوصول من مكان آخر",
        ),
        (
            "en",
            "About 45 km · Around 35 minutes by car",
            "Directions from the airport",
            "Directions from another location",
        ),
        (
            "fr",
            "Environ 45 km · Environ 35 minutes en voiture",
            "Itinéraire depuis l’aéroport",
            "Itinéraire depuis un autre lieu",
        ),
    ],
)
def test_arrival_block_follows_map_with_translations_and_safe_links(
    language: str, estimate: str, airport_label: str, other_label: str
) -> None:
    property_obj = make_property()
    with patch("requests.sessions.Session.request") as provider_request:
        response = Client().get(f"/{language}/properties/{property_obj.slug}/")
    provider_request.assert_not_called()
    assert response.status_code == 200
    content = unescape(response.content.decode())
    assert content.index("data-property-map") < content.index('class="property-arrival"')
    assert estimate in content
    assert airport_label in content
    assert other_label in content
    assert f'href="{AIRPORT_URL}" target="_blank" rel="noopener noreferrer"' in content
    block = content.split('class="property-arrival"', 1)[1].split("</section>", 1)[0]
    assert block.count('target="_blank" rel="noopener noreferrer"') == 2
    assert 'aria-describedby="airport-arrival-note"' in block
    assert "property-arrival.css" in content
    assert "google.com/maps?cid=" not in block
    if language != "en":
        assert "Directions from the airport" not in block
    property_obj.refresh_from_db()
    assert property_obj.currency_code == "SAR"
    assert property_obj.google_maps_cid == "4981474889453888860"


@pytest.mark.parametrize("listing_id", [11, 315816, 333333])
def test_other_properties_keep_their_existing_map_link(listing_id: int) -> None:
    property_obj = make_property(listing_id)
    response = Client().get(f"/ar/properties/{property_obj.slug}/")
    content = response.content.decode()
    assert response.status_code == 200
    assert "data-property-map" in content
    assert "google.com/maps?cid=4981474889453888860" in content
    assert 'class="property-arrival"' not in content
    assert "property-arrival.css" not in content
    assert "ChIJ_cSOVgAdLz4RXGVt2wTBIUU" not in content
    assert airport_directions(property_obj) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_location_enabled", False),
        ("public_location_latitude", None),
        ("public_location_longitude", None),
        ("google_maps_cid", "123456789"),
        ("google_maps_cid", ""),
    ],
)
def test_arrival_directions_fail_closed_when_location_is_not_verified(
    field: str, value: object
) -> None:
    property_obj = make_property()
    setattr(property_obj, field, value)
    assert airport_directions(property_obj) is None
    if field in {"public_location_latitude", "public_location_longitude"}:
        # Exercise an incomplete in-memory location, then persist the valid
        # disabled state required by the existing coordinate-pair constraints.
        property_obj.public_location_latitude = None
        property_obj.public_location_longitude = None
        property_obj.public_location_enabled = False
        property_obj.save(
            update_fields=[
                "public_location_latitude",
                "public_location_longitude",
                "public_location_enabled",
            ]
        )
    else:
        property_obj.save(update_fields=[field])
    content = Client().get(f"/ar/properties/{property_obj.slug}/").content.decode()
    assert 'class="property-arrival"' not in content
    assert "property-arrival.css" not in content
    assert "ChIJ_cSOVgAdLz4RXGVt2wTBIUU" not in content
