"""Allowlisted, one-shot traffic estimates. No Google response is stored or cached."""

import json
import logging
import math
import re
import secrets

import httpx
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from .directions import verified_airport_route
from .models import Property

logger = logging.getLogger(__name__)
TICKET_SALT = "property-airport-route.v1"
TICKET_MAX_AGE = 1800
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def make_route_ticket(property_id: int) -> str:
    return signing.dumps(
        {"property_id": property_id, "nonce": secrets.token_urlsafe(24)}, salt=TICKET_SALT
    )


def _over_limit(key: str, limit: int, seconds: int) -> bool:
    if cache.add(key, 1, timeout=seconds):
        return False
    try:
        return cache.incr(key) > limit
    except ValueError:
        # Fail closed if a key expires between operations, rather than resetting
        # it in a way that can silently grant concurrent paid requests.
        return True


def _reply(detail: str, status: int) -> JsonResponse:
    return JsonResponse({"detail": detail}, status=status)


@method_decorator(never_cache, name="dispatch")
class PropertyAirportRouteView(View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, slug: str) -> JsonResponse:
        property_obj = get_object_or_404(Property.objects.public(), slug=slug)
        route = verified_airport_route(property_obj)
        if route is None or not route.live_traffic:
            return _reply("route_not_available", 404)
        if not settings.GOOGLE_ROUTES_ENABLED or not settings.GOOGLE_ROUTES_API_KEY:
            return _reply("live_estimate_unavailable", 503)
        if settings.GOOGLE_ROUTES_REQUIRE_SHARED_CACHE and not settings.CACHES["default"][
            "BACKEND"
        ].endswith("RedisCache"):
            return _reply("live_estimate_unavailable", 503)
        if len(request.body) > 2048:
            return _reply("invalid_request", 400)
        try:
            payload = json.loads(request.body)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"ticket"}
                or not isinstance(payload["ticket"], str)
            ):
                return _reply("invalid_request", 400)
            ticket = signing.loads(payload["ticket"], salt=TICKET_SALT, max_age=TICKET_MAX_AGE)
            if (
                not isinstance(ticket, dict)
                or ticket.get("property_id") != property_obj.pk
                or not isinstance(ticket.get("nonce"), str)
                or len(ticket["nonce"]) != 32
            ):
                return _reply("invalid_ticket", 400)
        except (ValueError, TypeError, signing.BadSignature):
            return _reply("invalid_ticket", 400)

        # Metadata only: counters and ticket hashes, never API content or keys.
        try:
            client_hash = salted_hmac(
                "routes-client.v1", request.META.get("CSRF_COOKIE", "")
            ).hexdigest()
            if _over_limit(f"routes:client:{client_hash}", 3, 600):
                return _reply("rate_limited", 429)
            nonce_hash = salted_hmac("routes-ticket.v1", ticket["nonce"]).hexdigest()
            if not cache.add(f"routes:used:{nonce_hash}", True, timeout=TICKET_MAX_AGE):
                return _reply("ticket_used", 429)
            now = timezone.now()
            if _over_limit(f"routes:minute:{int(now.timestamp()) // 60}", 20, 120):
                return _reply("rate_limited", 429)
            if _over_limit(
                f"routes:day:{now.date().isoformat()}", settings.GOOGLE_ROUTES_DAILY_LIMIT, 90000
            ):
                return _reply("daily_limit_reached", 429)
        except Exception:
            # An unavailable cache must not turn the endpoint into an unmetered
            # provider proxy. Never log the ticket, IP, provider body or key.
            logger.warning("Airport route request rejected: abuse-control cache unavailable")
            return _reply("live_estimate_unavailable", 503)

        if route.airport_place_id:
            origin = {"placeId": route.airport_place_id}
        else:
            origin_latitude, origin_longitude = (
                float(value) for value in route.airport_origin.split(",")
            )
            origin = {
                "location": {"latLng": {"latitude": origin_latitude, "longitude": origin_longitude}}
            }
        try:
            with httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
                response = client.post(
                    ROUTES_URL,
                    headers={
                        "X-Goog-Api-Key": settings.GOOGLE_ROUTES_API_KEY,
                        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
                    },
                    json={
                        "origin": origin,
                        "destination": {"placeId": route.destination_place_id},
                        "travelMode": "DRIVE",
                        "routingPreference": "TRAFFIC_AWARE",
                        "computeAlternativeRoutes": False,
                        # Google uses the instant it receives the request when
                        # departureTime is omitted; never send a past timestamp.
                        "units": "METRIC",
                    },
                )
                response.raise_for_status()
                routes = response.json()["routes"]
                result = routes[0]
                duration = result["duration"]
                distance = result["distanceMeters"]
                if not isinstance(duration, str) or not re.fullmatch(r"\d+(?:\.\d+)?s", duration):
                    raise ValueError("Invalid duration")
                seconds = float(duration[:-1])
                if (
                    isinstance(distance, bool)
                    or not isinstance(distance, int)
                    or not 0 < distance < 500000
                    or not math.isfinite(seconds)
                    or not 0 < seconds < 86400
                ):
                    raise ValueError("Invalid route values")
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, OverflowError):
            logger.warning("Airport route estimate unavailable from Google Routes")
            return _reply("live_estimate_unavailable", 503)
        response = JsonResponse(
            {
                "duration_minutes": math.ceil(seconds / 60),
                "distance_km": round(distance / 1000, 1),
                "calculated_at": timezone.now().isoformat(),
                "attribution": "Google Maps",
            }
        )
        response["X-Robots-Tag"] = "noindex"
        return response
