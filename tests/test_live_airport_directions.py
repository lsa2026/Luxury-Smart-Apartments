import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.properties.directions import airport_directions
from apps.properties.live_directions import make_route_ticket
from tests.test_property_airport_directions import make_property

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_routes(settings):
    settings.GOOGLE_ROUTES_ENABLED = True
    settings.GOOGLE_ROUTES_API_KEY = "test-only-server-key"
    settings.GOOGLE_ROUTES_DAILY_LIMIT = 150
    settings.GOOGLE_ROUTES_REQUIRE_SHARED_CACHE = False
    cache.clear()
    yield
    cache.clear()


def provider_result():
    response = MagicMock()
    response.json.return_value = {"routes": [{"duration": "1651s", "distanceMeters": 35123}]}
    client = MagicMock()
    client.__enter__.return_value.post.return_value = response
    return client, response


def post_route(client, property_obj, ticket=None, **kwargs):
    return client.post(
        f"/ar/properties/{property_obj.slug}/airport-route/",
        json.dumps({"ticket": ticket or make_route_ticket(property_obj.pk)}),
        content_type="application/json",
        **kwargs,
    )


@pytest.mark.parametrize(
    ("listing_id", "place_id"),
    [
        (315815, "ChIJXY-ywnLnLj4R6TKd37VpY-M"),
        (315816, "ChIJl-vqdXvjLj4RCIYB5DldC5k"),
        (325961, "ChIJsy-YIQD_Lj4RIUmilSxYynI"),
        (343666, "ChIJVaDhVZD_Lj4RES0Nej_3-XI"),
        (511786, "ChIJ54Web3jvrw0RENTa4RVQ0uE"),
    ],
)
def test_exact_destination_traffic_time_and_no_google_content_cache(listing_id, place_id):
    property_obj = make_property(listing_id)
    provider, _ = provider_result()
    with patch("apps.properties.live_directions.httpx.Client", return_value=provider):
        response = post_route(Client(), property_obj)
    assert response.status_code == 200
    result = response.json()
    assert result["duration_minutes"] == 28
    assert result["distance_km"] == 35.1
    assert result["attribution"] == "Google Maps"
    assert (timezone.now() - timezone.datetime.fromisoformat(result["calculated_at"])).seconds < 5
    _, kwargs = provider.__enter__.return_value.post.call_args
    assert kwargs["json"]["destination"] == {"placeId": place_id}
    if listing_id == 511786:
        assert kwargs["json"]["origin"] == {"placeId": "ChIJdcWwntDurw0R5589e1uB9cM"}
        assert "24.959443" not in str(kwargs)
    else:
        assert kwargs["json"]["origin"]["location"]["latLng"] == {
            "latitude": 24.959443,
            "longitude": 46.7010829,
        }
    assert kwargs["json"]["routingPreference"] == "TRAFFIC_AWARE"
    assert "departureTime" not in kwargs["json"]
    assert kwargs["json"]["computeAlternativeRoutes"] is False
    assert kwargs["headers"]["X-Goog-FieldMask"] == "routes.duration,routes.distanceMeters"
    assert "no-store" in response["Cache-Control"]
    assert "test-only-server-key" not in response.content.decode()
    assert "ChIJ_cSOVgAdLz4RXGVt2wTBIUU" not in str(kwargs)
    assert "duration" not in str(cache._cache)


def test_one_ticket_can_never_create_two_paid_calls():
    property_obj = make_property(315815)
    ticket = make_route_ticket(property_obj.pk)
    provider, _ = provider_result()
    with patch("apps.properties.live_directions.httpx.Client", return_value=provider):
        assert post_route(Client(), property_obj, ticket).status_code == 200
        assert post_route(Client(), property_obj, ticket).status_code == 429
    assert provider.__enter__.return_value.post.call_count == 1


@pytest.mark.parametrize("value", [None, 42, {}, "", "forged", "x" * 3000])
def test_bad_tickets_never_contact_google(value):
    property_obj = make_property(315815)
    with patch("apps.properties.live_directions.httpx.Client") as provider:
        response = Client().post(
            f"/en/properties/{property_obj.slug}/airport-route/",
            json.dumps({"ticket": value}),
            content_type="application/json",
        )
    assert response.status_code == 400
    provider.assert_not_called()


def test_ticket_is_bound_to_property_and_expires():
    property_obj = make_property(315815)
    with patch("apps.properties.live_directions.httpx.Client") as provider:
        assert post_route(Client(), property_obj, make_route_ticket(999999)).status_code == 400
        with patch("django.core.signing.time.time", return_value=1):
            expired = make_route_ticket(property_obj.pk)
        assert post_route(Client(), property_obj, expired).status_code == 400
    provider.assert_not_called()


def test_unrequested_origin_query_and_no_csrf_cannot_create_paid_call():
    property_obj = make_property(315815)
    url = f"/ar/properties/{property_obj.slug}/airport-route/"
    with patch("apps.properties.live_directions.httpx.Client") as provider:
        assert Client().get(url).status_code == 405
        assert Client(enforce_csrf_checks=True).post(url, {"ticket": "x"}).status_code == 403
        response = Client().post(
            url,
            json.dumps({"ticket": make_route_ticket(property_obj.pk), "origin": "Paris"}),
            content_type="application/json",
        )
        assert response.status_code == 400
    provider.assert_not_called()


def test_daily_limit_bounds_provider_cost(settings):
    property_obj = make_property(315815)
    settings.GOOGLE_ROUTES_DAILY_LIMIT = 1
    provider, _ = provider_result()
    with patch("apps.properties.live_directions.httpx.Client", return_value=provider):
        assert post_route(Client(), property_obj, REMOTE_ADDR="192.0.2.1").status_code == 200
        response = post_route(Client(), property_obj, REMOTE_ADDR="192.0.2.2")
        assert response.status_code == 429
        assert response.json()["detail"] == "daily_limit_reached"
    assert provider.__enter__.return_value.post.call_count == 1


def test_browser_cap_does_not_block_other_visitors_behind_the_same_proxy():
    property_obj = make_property(315815)
    first_browser, second_browser = Client(), Client()
    first_browser.get(f"/ar/properties/{property_obj.slug}/")
    second_browser.get(f"/ar/properties/{property_obj.slug}/")
    assert first_browser.cookies["csrftoken"].value != second_browser.cookies["csrftoken"].value
    provider, _ = provider_result()
    with patch("apps.properties.live_directions.httpx.Client", return_value=provider):
        for _ in range(3):
            assert (
                post_route(first_browser, property_obj, REMOTE_ADDR="192.0.2.1").status_code == 200
            )
        assert post_route(first_browser, property_obj, REMOTE_ADDR="192.0.2.1").status_code == 429
        assert post_route(second_browser, property_obj, REMOTE_ADDR="192.0.2.1").status_code == 200
    assert provider.__enter__.return_value.post.call_count == 4


def test_missing_shared_cache_fails_closed(settings):
    property_obj = make_property(315815)
    settings.GOOGLE_ROUTES_REQUIRE_SHARED_CACHE = True
    with patch("apps.properties.live_directions.httpx.Client") as provider:
        assert post_route(Client(), property_obj).status_code == 503
    provider.assert_not_called()


@pytest.mark.parametrize("failure", ["timeout", "empty", "invalid", "negative", "bool", "cache"])
def test_provider_and_cache_failures_never_fabricate_success(failure):
    property_obj = make_property(315815)
    provider, response = provider_result()
    if failure == "timeout":
        provider.__enter__.return_value.post.side_effect = httpx.ReadTimeout("test timeout")
    elif failure == "empty":
        response.json.return_value = {"routes": []}
    elif failure == "invalid":
        response.json.return_value = {"routes": [{"duration": "nan", "distanceMeters": 30}]}
    elif failure == "negative":
        response.json.return_value = {"routes": [{"duration": "-50s", "distanceMeters": 30}]}
    elif failure == "bool":
        response.json.return_value = {"routes": [{"duration": "50s", "distanceMeters": True}]}
    with patch("apps.properties.live_directions.httpx.Client", return_value=provider):
        if failure == "cache":
            with patch("apps.properties.live_directions.cache.add", side_effect=RuntimeError):
                result = post_route(Client(), property_obj)
        else:
            result = post_route(Client(), property_obj)
    assert result.status_code == 503
    assert "duration_minutes" not in result.json()
    assert "no-store" in result["Cache-Control"]


@pytest.mark.parametrize("listing_id", [315814, 325731])
def test_live_estimate_is_not_enabled_for_unauthorized_properties(listing_id):
    property_obj = make_property(listing_id)
    with patch("apps.properties.live_directions.httpx.Client") as provider:
        assert post_route(Client(), property_obj).status_code == 404
    provider.assert_not_called()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("listing_id", [315815, 315816, 325961, 343666, 511786])
def test_page_contains_only_lazy_server_ticket_no_api_key_no_static_darat_number(
    language, listing_id
):
    property_obj = make_property(listing_id)
    with patch("apps.properties.live_directions.httpx.Client") as provider:
        result = Client().get(f"/{language}/properties/{property_obj.slug}/")
    provider.assert_not_called()
    content = result.content.decode()
    assert "data-airport-live" in content
    assert "airport-directions.js" in content
    assert 'type="module"' in content
    assert "test-only-server-key" not in content
    assert "35 km" not in content
    assert "45 km" not in content
    assert "31.4 km" not in content
    assert "geolocation" not in content
    assert "route_ticket" in airport_directions(property_obj)


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("page", ["terms", "privacy"])
def test_requested_map_notices_are_removed_from_public_documents(language, page):
    content = Client().get(f"/{language}/legal/{page}/").content.decode()
    assert "https://maps.google.com/help/terms_maps/" not in content
    assert "Google Maps" not in content
    for heading in (
        "Google Maps driving estimates",
        "Payment address suggestions",
        "تقديرات القيادة من خرائط Google",
        "اقتراحات عنوان الدفع",
        "Estimations de trajet Google Maps",
        "Suggestions d’adresse de paiement",
    ):
        assert heading not in content
    if page == "privacy":
        # Unrelated optional analytics disclosures remain intact.
        assert "Google Analytics" in content
        assert "https://policies.google.com/privacy" in content
