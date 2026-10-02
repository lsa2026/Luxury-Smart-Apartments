from urllib.parse import quote, urlencode

from .models import Property


def airport_directions(property_obj: Property) -> dict[str, str] | None:
    """Ordinary Maps links for the verified Safa 41 B1 location only; no API calls."""
    if (
        property_obj.hostaway_listing_id != 315814
        or property_obj.google_maps_cid != "4981474889453888860"
        or not property_obj.public_location_enabled
        or property_obj.public_location_latitude is None
        or property_obj.public_location_longitude is None
    ):
        return None

    destination = {
        "destination": "Luxury Smart Apartment Safa 41 B1",
        "destination_place_id": "ChIJ_cSOVgAdLz4RXGVt2wTBIUU",
        "travelmode": "driving",
    }
    base_url = "https://www.google.com/maps/dir/?"
    airport_query = urlencode(
        {"api": 1, "origin": "24.959443,46.7010829", **destination},
        quote_via=quote,
        safe=",",
    )
    other_origin_query = urlencode({"api": 1, **destination}, quote_via=quote)
    return {
        "airport_url": base_url + airport_query,
        "other_origin_url": base_url + other_origin_query,
    }
