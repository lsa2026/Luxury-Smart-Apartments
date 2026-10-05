import hashlib
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.test import Client

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayResponseError
from apps.reservations import sama_booking_api as api
from apps.reservations.models import Reservation
from apps.reservations.services import sama_guest_context as service
from tests.test_booking_models_services import make_property
from tests.test_sama_booking_api import ENABLED, HEADERS, post

pytestmark = pytest.mark.django_db
PHONE = "+966500000009"
NOW = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)


@pytest.fixture
def context(monkeypatch):
    cache.clear()
    monkeypatch.setattr(service.timezone, "now", lambda: NOW)
    p = make_property()
    p.hostaway_listing_map_id, p.country_code, p.time_zone_name = 123, "SA", "Asia/Riyadh"
    p.save()
    row = {
        "id": 111,
        "listingMapId": 123,
        "phone": PHONE,
        "status": "new",
        "arrivalDate": "2026-10-03",
        "departureDate": "2026-10-05",
        "guestName": "NEVER_KEEP",
        "guestEmail": "NEVER_KEEP",
        "doorCode": "NEVER_KEEP",
    }
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.guest_stay_page.return_value = [row]
    factory = Mock(return_value=client)
    yield p, row, client, factory
    cache.clear()


def test_exact_match_for_all_channels_without_database_writes_or_guest_details(context):
    p, row, client, factory = context
    result = service.current_guest_context(PHONE, client_factory=factory)
    assert result["stays"][0]["phase"] == "current"
    assert result["stays"][0]["property_slug"] == p.slug
    assert result["match_key"] == hashlib.sha256(PHONE.encode()).hexdigest()
    assert PHONE not in str(result) and "NEVER_KEEP" not in str(result)
    assert Reservation.objects.count() == 0
    factory.assert_called_once_with(max_get_attempts=1, timeout=4)
    assert service.current_guest_context("+966500000008", client_factory=factory)["stays"] == []
    assert client.guest_stay_page.call_count == 1
    assert "NEVER_KEEP" not in str(cache.get("sama-guest-context-v1:2026-10-03:2026-10-05"))


@pytest.mark.parametrize(
    "status", ["cancelled", "declined", "pending", "inquiry", "awaitingPayment", "unknown"]
)
def test_non_active_bookings_never_become_current_guests(context, status):
    p, row, client, factory = context
    row["status"] = status
    assert not service.current_guest_context(PHONE, client_factory=factory)["stays"]


@pytest.mark.parametrize(
    "value", ["+966 50 000 0009", "00966500000009", "966500000009", "٠٠٩٦٦٥٠٠٠٠٠٠٠٩"]
)
def test_international_phone_format_normalization(value):
    assert service.canonical_phone(value) == PHONE


@pytest.mark.parametrize("value", [None, "0500000009", "000009", "number <script>"])
def test_partial_phone_does_not_guess_a_country(value):
    assert service.canonical_phone(value) is None


def test_local_checkin_checkout_boundaries_and_morocco_timezone(context):
    p, row, client, factory = context
    row["arrivalDate"] = "2026-10-04"
    # At Riyadh noon, today's standard 15:00 arrival is still upcoming.
    assert (
        service.current_guest_context(PHONE, client_factory=factory)["stays"][0]["phase"]
        == "before_arrival"
    )
    cache.clear()
    row["arrivalDate"], row["departureDate"] = "2026-10-03", "2026-10-04"
    # Riyadh noon is exactly checkout; the interval is end-exclusive.
    assert (
        service.current_guest_context(PHONE, client_factory=factory)["stays"][0]["phase"]
        == "departed"
    )
    cache.clear()
    p.country_code, p.time_zone_name = "MA", "Africa/Casablanca"
    p.save()
    assert (
        service.current_guest_context(PHONE, client_factory=factory)["stays"][0]["phase"]
        == "current"
    )


def test_repeated_or_truncated_pages_do_not_claim_complete_context(context):
    p, row, client, factory = context
    client.guest_stay_page.return_value = [{**row, "id": i} for i in range(1, 101)]
    with pytest.raises(HostawayResponseError):
        service.current_guest_context(PHONE, client_factory=factory)
    assert client.guest_stay_page.call_count == 2


@ENABLED
def test_lookup_requires_existing_bearer_identity_and_https_and_never_echoes_phone(monkeypatch):
    fn = Mock(return_value={"code": "guest_context", "source": "hostaway_read_only", "stays": []})
    monkeypatch.setattr(api, "current_guest_context", fn)
    path = "/reservations/sama/guest-context/"
    assert (
        Client()
        .post(path, {"phone": PHONE}, content_type="application/json", secure=True)
        .status_code
        == 401
    )
    assert (
        Client()
        .post(path, {"phone": PHONE}, content_type="application/json", **HEADERS)
        .status_code
        == 403
    )
    assert Client().get(path, secure=True, **HEADERS).status_code == 405
    for value in [None, "0500000009", "bad"]:
        assert post(path, {"phone": value}).status_code == 400
    fn.assert_not_called()
    result = post(path, {"phone": PHONE})
    assert result.status_code == 200 and result["Cache-Control"] == "no-store"
    fn.assert_called_once_with(PHONE)
    assert PHONE not in result.content.decode()


@ENABLED
def test_hostaway_error_is_unknown_not_not_a_guest_and_cannot_leak_details(monkeypatch):
    monkeypatch.setattr(
        api, "current_guest_context", Mock(side_effect=TimeoutError("SECRET_PHONE_AND_URL"))
    )
    result = post("/reservations/sama/guest-context/", {"phone": PHONE})
    assert result.status_code == 503 and result.json()["code"] == "review_required"
    assert "SECRET" not in result.content.decode()


def test_documented_hostaway_reader_uses_date_filters_and_cursor_without_phone():
    client = object.__new__(HostawayClient)
    client._get_json = Mock(return_value={"status": "success", "result": []})
    client.guest_stay_page(NOW.date(), NOW.date(), after_id=123)
    args, kwargs = client._get_json.call_args
    assert args == ("/reservations",)
    assert dict(kwargs["params"]) == {
        "limit": 100,
        "includeResources": 0,
        "departureStartDate": "2026-10-04",
        "arrivalEndDate": "2026-10-04",
        "afterId": 123,
    }
    assert PHONE not in str(client._get_json.call_args)


@pytest.mark.parametrize(
    "value", [{"status": "error", "result": []}, {"status": "success", "result": [{}] * 101}]
)
def test_hostaway_reader_rejects_failure_and_unbounded_pages(value):
    client = object.__new__(HostawayClient)
    client._get_json = Mock(return_value=value)
    with pytest.raises(HostawayResponseError):
        client.guest_stay_page(NOW.date(), NOW.date())
