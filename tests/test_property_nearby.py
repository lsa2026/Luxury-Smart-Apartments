import json
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import Client, override_settings

from apps.properties.directions import VERIFIED_AIRPORT_ROUTES
from apps.properties.nearby import nearby_config
from tests.test_property_airport_directions import make_property

pytestmark = pytest.mark.django_db
ROOT = Path(__file__).parents[1]
RELEASE = {
    p["fields"]["hostaway_listing_id"]: p["fields"]
    for p in json.loads(
        (ROOT / "apps/properties/data/release_properties.json").read_text(encoding="utf-8")
    )
    if p["model"] == "properties.property"
}


@pytest.mark.parametrize("listing_id", VERIFIED_AIRPORT_ROUTES)
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@override_settings(
    GOOGLE_NEARBY_ENABLED=True,
    GOOGLE_MAPS_BROWSER_API_KEY="referrer-browser-key",
    GOOGLE_ROUTES_API_KEY="private-server-routes",
    GOOGLE_PLACES_API_KEY="private-server-places",
)
def test_nearby_uses_each_public_location_and_locale_without_provider_calls(listing_id, language):
    p = make_property(listing_id)
    release = RELEASE[listing_id]
    p.public_location_latitude = Decimal(release["public_location_latitude"])
    p.public_location_longitude = Decimal(release["public_location_longitude"])
    p.save()
    with patch("requests.sessions.Session.request", side_effect=AssertionError("No Google call")):
        response = Client().get(f"/{language}/properties/{p.slug}/")
    assert response.status_code == 200
    html = response.content.decode()
    config = json.loads(
        re.search(
            r'<script id="property-nearby-config" type="application/json">(.*?)</script>', html
        )[1]
    )
    assert config["center"] == {
        "lat": float(p.public_location_latitude),
        "lng": float(p.public_location_longitude),
    }
    assert config["language"] == language
    assert config["region"] == ("MA" if listing_id == 511786 else "SA")
    assert config["key"] == "referrer-browser-key"
    assert "private-server" not in html
    assert html.count("data-nearby-category=") == 8
    if language == "en":
        assert html.index("Directions from another location") < html.index("data-nearby-open")
    assert "maps.googleapis.com/maps/api/js" not in html
    assert "leaflet.js" in html and "property-map.js" in html
    assert "maps.googleapis.com" in response["Content-Security-Policy"]
    for text in {
        "ar": ["اكتشف ما حول الشقة", "العودة إلى الشقة", "ابحث في هذه المنطقة"],
        "en": ["Discover nearby", "Back to apartment", "Search this area"],
        "fr": ["Découvrez les environs", "Retour à l’appartement", "Rechercher dans cette zone"],
    }[language]:
        assert text in html


@pytest.mark.parametrize("enabled,key", [(False, "browser"), (True, ""), (False, "")])
def test_disabled_nearby_has_no_assets_config_or_csp_exception(enabled, key):
    p = make_property()
    with override_settings(GOOGLE_NEARBY_ENABLED=enabled, GOOGLE_MAPS_BROWSER_API_KEY=key):
        response = Client().get(f"/en/properties/{p.slug}/")
    assert "data-nearby-open" not in response.content.decode()
    assert "property-nearby.js" not in response.content.decode()
    assert "maps.googleapis.com" not in response["Content-Security-Policy"]
    assert "Directions from another location" in response.content.decode()


@pytest.mark.parametrize("invalid", ["unknown", "wrong-cid", "disabled", "missing", "range"])
@override_settings(GOOGLE_NEARBY_ENABLED=True, GOOGLE_MAPS_BROWSER_API_KEY="browser")
def test_unknown_or_unverified_location_fails_closed(invalid):
    p = make_property(999 if invalid == "unknown" else 315814)
    if invalid == "wrong-cid":
        p.google_maps_cid = VERIFIED_AIRPORT_ROUTES[315815].google_maps_cid
    if invalid == "disabled":
        p.public_location_enabled = False
    if invalid == "missing":
        p.public_location_latitude = None
    if invalid == "range":
        p.public_location_latitude = Decimal(91)
    assert nearby_config(p) is None


@override_settings(GOOGLE_NEARBY_ENABLED=True, GOOGLE_MAPS_BROWSER_API_KEY="browser")
def test_nearby_csp_is_not_enabled_on_home_or_booking_pages():
    response = Client().get("/en/")
    assert "maps.googleapis.com" not in response["Content-Security-Policy"]
    assert "'unsafe-eval'" not in response["Content-Security-Policy"]


@override_settings(GOOGLE_NEARBY_ENABLED=True, GOOGLE_MAPS_BROWSER_API_KEY="browser</script>")
def test_config_is_script_safe_and_no_migrations_needed():
    p = make_property()
    html = Client().get(f"/en/properties/{p.slug}/").content.decode()
    assert "browser</script>" not in html
    assert r"browser\u003C/script\u003E" in html
