from dataclasses import dataclass
from urllib.parse import quote, urlencode

from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext as _

from .models import Property


@dataclass(frozen=True)
class AirportRoute:
    google_maps_cid: str
    destination: str
    destination_place_id: str
    distance_km: int | None = None
    drive_minutes: int | None = None
    live_traffic: bool = False
    airport_origin: str = "24.959443,46.7010829"


# Only individually reviewed routes are published. The shared presentation
# must never assume all properties have the same destination or airport distance.
VERIFIED_AIRPORT_ROUTES = {
    315814: AirportRoute(
        google_maps_cid="4981474889453888860",
        destination="Luxury Smart Apartment Safa 41 B1",
        destination_place_id="ChIJ_cSOVgAdLz4RXGVt2wTBIUU",
        distance_km=45,
        drive_minutes=35,
    ),
    315815: AirportRoute(
        google_maps_cid="16385056099165614825",
        destination="Darat Safa Luxury Smart Apartment D 203",
        destination_place_id="ChIJXY-ywnLnLj4R6TKd37VpY-M",
        live_traffic=True,
    ),
    315816: AirportRoute(
        google_maps_cid="11028010615766615560",
        destination="Luxury Smart Apartment E12",
        destination_place_id="ChIJl-vqdXvjLj4RCIYB5DldC5k",
        live_traffic=True,
    ),
    325961: AirportRoute(
        google_maps_cid="8271520614131583265",
        destination="Luxury Smart Apartment A11",
        destination_place_id="ChIJsy-YIQD_Lj4RIUmilSxYynI",
        live_traffic=True,
    ),
}


def verified_airport_route(property_obj: Property) -> AirportRoute | None:
    route = VERIFIED_AIRPORT_ROUTES.get(property_obj.hostaway_listing_id)
    if (
        route is None
        or property_obj.google_maps_cid != route.google_maps_cid
        or not property_obj.public_location_enabled
        or property_obj.public_location_latitude is None
        or property_obj.public_location_longitude is None
    ):
        return None
    return route


def airport_directions(property_obj: Property) -> dict[str, object] | None:
    """Prepare links and a short-lived ticket; never call Google while rendering."""
    route = verified_airport_route(property_obj)
    if route is None:
        return None

    destination = {
        "destination": route.destination,
        "destination_place_id": route.destination_place_id,
        "travelmode": "driving",
    }
    base_url = "https://www.google.com/maps/dir/?"
    airport_query = urlencode(
        {"api": 1, "origin": route.airport_origin, **destination},
        quote_via=quote,
        safe=",",
    )
    other_origin_query = urlencode({"api": 1, **destination}, quote_via=quote)
    result: dict[str, object] = {
        "airport_url": base_url + airport_query,
        "other_origin_url": base_url + other_origin_query,
        "live_traffic": route.live_traffic,
    }
    if route.live_traffic:
        from .live_directions import make_route_ticket

        enabled = settings.GOOGLE_ROUTES_ENABLED and bool(settings.GOOGLE_ROUTES_API_KEY)
        result.update(
            live_enabled=enabled,
            route_url=reverse("properties:airport_route", kwargs={"slug": property_obj.slug}),
            route_ticket=make_route_ticket(property_obj.pk) if enabled else "",
        )
    else:
        result["estimate"] = _("About %(distance)s km · Around %(duration)s minutes by car") % {
            "distance": route.distance_km,
            "duration": route.drive_minutes,
        }
    return result
