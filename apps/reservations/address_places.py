"""Checkout-only Google Places (New). Cache metadata, never address content."""

import json
import logging
import re
import uuid

import httpx
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import Http404, HttpRequest, JsonResponse
from django.utils import timezone, translation
from django.utils.crypto import salted_hmac
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from apps.payments.countries import ISO_ALPHA2_COUNTRY_CODES

from .models import BookingQuote
from .security import session_owns
from .signing import quote_id_from_reference, verify_quote_fingerprint

logger = logging.getLogger(__name__)
TICKET_MAX_AGE = 600
BASE_URL = "https://places.googleapis.com/v1/places"
AUTOCOMPLETE_MASK = "suggestions.placePrediction.placeId,suggestions.placePrediction.text.text"
DETAILS_MASK = "addressComponents,formattedAddress"
PLACE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")
POSTCODE = re.compile(r"[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*")


def places_enabled() -> bool:
    return bool(
        settings.GOOGLE_PLACES_ENABLED
        and settings.GOOGLE_PLACES_API_KEY
        and (
            not settings.GOOGLE_PLACES_REQUIRE_SHARED_CACHE
            or settings.CACHES["default"]["BACKEND"].endswith("RedisCache")
        )
    )


def _salt(quote: BookingQuote) -> str:
    # Also bind the signed token to this quote and its owning browser session,
    # without exposing the session hash in page HTML or JSON.
    return f"checkout-places.v1:{quote.pk}:{quote.session_key_hash}"


def make_address_ticket(quote: BookingQuote) -> str:
    language = (translation.get_language() or "en").split("-")[0]
    return signing.dumps(
        {
            "nonce": str(uuid.uuid4()),
            "language": language if language in {"ar", "en", "fr"} else "en",
        },
        salt=_salt(quote),
    )


def _reply(status: int = 200, **body: object) -> JsonResponse:
    response = JsonResponse(body)
    response.status_code = status
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _over_limit(key: str, limit: int, seconds: int) -> bool:
    if cache.add(key, 1, timeout=seconds):
        return False
    try:
        return cache.incr(key) > limit
    except ValueError:
        return True  # Fail closed on eviction / concurrent expiry.


def _key(kind: str, value: str) -> str:
    return f"places:{kind}:{salted_hmac('checkout-places-metadata.v1', value).hexdigest()}"


def _reserve_request(request: HttpRequest, quote: BookingQuote, nonce: str) -> bool:
    now = timezone.now()
    limits = (
        (_key("client", quote.session_key_hash), 25, 600),
        (_key("quote", str(quote.pk)), 40, 3600),
        (_key("ip", request.META.get("REMOTE_ADDR", "")), 60, 600),
        (_key("session", nonce), 20, TICKET_MAX_AGE),
        (f"places:minute:{int(now.timestamp()) // 60}", 60, 120),
        # A combined daily allowance, independent of Google Routes. This
        # counts reservations before calls (including failures), not users.
        (f"places:day:{now.date().isoformat()}", settings.GOOGLE_PLACES_DAILY_LIMIT, 90000),
    )
    return not any(_over_limit(key, limit, seconds) for key, limit, seconds in limits)


def address_fields(data: dict) -> dict[str, str]:
    """Only usable components; never invent or truncate a required field."""
    components: dict[str, str] = {}
    items = data.get("addressComponents", [])
    if not isinstance(items, list):
        return {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("types"), list):
            continue
        text = item.get("longText")
        if not isinstance(text, str) or not text.strip() or not text.isprintable():
            continue
        for kind in item["types"]:
            if isinstance(kind, str):
                components[kind] = text.strip()
        if "country" in item["types"]:
            code = item.get("shortText")
            if isinstance(code, str) and code.upper() in ISO_ALPHA2_COUNTRY_CODES:
                components["country_code"] = code.upper()
        if "administrative_area_level_1" in item["types"]:
            region = item.get("shortText") or text.strip()
            # Prefer the provider's actual subdivision abbreviation (e.g.
            # NY, ON, IDF). No guessed ISO codes or deletion of Arabic letters.
            # Details is requested in English for gateway interoperability.
            if isinstance(region, str) and re.fullmatch(r"[A-Za-z0-9. -]{1,50}", region):
                components["payment_region"] = region
    street = (
        " ".join(components.get(kind, "") for kind in ("street_number", "route")).strip()
        if components.get("route")
        else ""
    )
    postal = components.get("postal_code", "")
    suffix = components.get("postal_code_suffix", "")
    if suffix and components.get("country_code") == "US":
        postal = f"{postal}-{suffix}" if postal else ""
    values = {
        "billing_street1": (street, 100),
        "billing_city": (components.get("postal_town") or components.get("locality", ""), 80),
        "billing_state": (components.get("payment_region", ""), 50),
        "billing_country": (components.get("country_code", ""), 2),
        "billing_postcode": (postal if POSTCODE.fullmatch(postal) else "", 16),
    }
    return {name: value for name, (value, limit) in values.items() if value and len(value) <= limit}


@method_decorator(never_cache, name="dispatch")
class CheckoutAddressView(View):
    http_method_names = ["post"]
    details = False

    def post(self, request: HttpRequest, reference: str) -> JsonResponse:
        try:
            quote = BookingQuote.objects.get(pk=quote_id_from_reference(reference))
        except (signing.BadSignature, ValueError, BookingQuote.DoesNotExist) as exc:
            raise Http404 from exc
        if not session_owns(request, quote.session_key_hash):
            raise Http404
        if (
            quote.status != BookingQuote.Status.ACTIVE
            or quote.is_expired
            or not verify_quote_fingerprint(quote)
        ):
            return _reply(409, detail="quote_unavailable")
        if not places_enabled():
            return _reply(503, detail="address_search_unavailable")
        field = "place_id" if self.details else "input"
        if len(request.body) > 4096 or request.content_type != "application/json":
            return _reply(400, detail="invalid_request")
        try:
            payload = json.loads(request.body)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"ticket", field}
                or not all(isinstance(value, str) for value in payload.values())
            ):
                return _reply(400, detail="invalid_request")
            ticket = signing.loads(payload["ticket"], salt=_salt(quote), max_age=TICKET_MAX_AGE)
            nonce = ticket["nonce"]
            if str(uuid.UUID(nonce, version=4)) != nonce or ticket["language"] not in {
                "ar",
                "en",
                "fr",
            }:
                return _reply(400, detail="invalid_ticket")
        except signing.SignatureExpired:
            return _reply(409, detail="session_expired", next_ticket=make_address_ticket(quote))
        except (ValueError, TypeError, KeyError, signing.BadSignature):
            return _reply(400, detail="invalid_ticket")
        value = payload[field].strip()
        if (self.details and not PLACE_ID.fullmatch(value)) or (
            not self.details and (not 3 <= len(value) <= 150 or not value.isprintable())
        ):
            return _reply(400, detail="invalid_request")
        next_ticket = make_address_ticket(quote) if self.details else ""
        try:
            used_key = _key("used", nonce)
            if cache.get(used_key):
                return _reply(409, detail="session_expired", next_ticket=make_address_ticket(quote))
            if self.details:
                if not cache.get(_key("prediction", f"{nonce}:{value}")):
                    return _reply(400, detail="invalid_selection")
                if not cache.add(used_key, True, timeout=TICKET_MAX_AGE):
                    return _reply(409, detail="session_expired", next_ticket=next_ticket)
            if not _reserve_request(request, quote, nonce):
                return _reply(429, detail="rate_limited", next_ticket=next_ticket)
        except Exception:
            # Do not include input, Google responses, credentials, tickets or
            # addresses in diagnostic messages / exception traces.
            logger.warning("Checkout address search unavailable: abuse-control cache")
            return _reply(503, detail="address_search_unavailable", next_ticket=next_ticket)
        try:
            with httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
                headers = {
                    "X-Goog-Api-Key": settings.GOOGLE_PLACES_API_KEY,
                    "X-Goog-FieldMask": DETAILS_MASK if self.details else AUTOCOMPLETE_MASK,
                }
                if self.details:
                    response = client.get(
                        f"{BASE_URL}/{value}",
                        headers=headers,
                        # Keep search/results localized; standardized English
                        # components avoid unsupported non-Latin payment-state
                        # values. Country and subdivision codes remain exact.
                        params={"sessionToken": nonce, "languageCode": "en"},
                    )
                else:
                    response = client.post(
                        f"{BASE_URL}:autocomplete",
                        headers=headers,
                        json={
                            "input": value,
                            "sessionToken": nonce,
                            "languageCode": ticket["language"],
                            # Neutral worldwide bias, not the Render server's
                            # IP location and not the apartment's country.
                            "locationBias": {
                                "rectangle": {
                                    "low": {"latitude": -90, "longitude": -180},
                                    "high": {"latitude": 90, "longitude": 180},
                                }
                            },
                        },
                    )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError
                if self.details:
                    return _reply(fields=address_fields(data), next_ticket=next_ticket)
                suggestions = []
                for item in data.get("suggestions", [])[:5]:
                    prediction = item["placePrediction"]
                    place_id, label = prediction["placeId"], prediction["text"]["text"]
                    if (
                        not isinstance(place_id, str)
                        or not PLACE_ID.fullmatch(place_id)
                        or not isinstance(label, str)
                        or not 1 <= len(label) <= 500
                    ):
                        continue
                    # The only provider content cached is an allowed place ID's
                    # digest, scoped to this short-lived search session.
                    cache.set(
                        _key("prediction", f"{nonce}:{place_id}"), True, timeout=TICKET_MAX_AGE
                    )
                    suggestions.append({"place_id": place_id, "label": label})
                return _reply(suggestions=suggestions)
        except Exception:
            # Includes cache failure after Google returns. Never retry a paid
            # call automatically or expose provider exceptions to the guest.
            logger.warning("Checkout address search unavailable from Google Places")
            return _reply(503, detail="address_search_unavailable", next_ticket=next_ticket)


class CheckoutAddressDetailsView(CheckoutAddressView):
    details = True
