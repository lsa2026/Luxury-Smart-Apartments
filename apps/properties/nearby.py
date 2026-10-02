"""Opt-in, browser-only discovery; never call Google while rendering a page."""

from math import isfinite

from django.conf import settings
from django.utils.translation import get_language

from .directions import verified_airport_route
from .models import Property


def nearby_config(property_obj: Property) -> dict[str, object] | None:
    if not settings.GOOGLE_NEARBY_ENABLED or not settings.GOOGLE_MAPS_BROWSER_API_KEY:
        return None
    route = verified_airport_route(property_obj)
    if route is None:
        return None
    latitude = float(property_obj.public_location_latitude)
    longitude = float(property_obj.public_location_longitude)
    if not (
        isfinite(latitude)
        and isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        return None
    language = (get_language() or "en").split("-")[0]
    return {
        "key": settings.GOOGLE_MAPS_BROWSER_API_KEY,
        "mapId": settings.GOOGLE_NEARBY_MAP_ID,
        "center": {"lat": latitude, "lng": longitude},
        "language": language if language in {"ar", "en", "fr"} else "en",
        "region": "MA" if property_obj.hostaway_listing_id == 511786 else "SA",
    }
