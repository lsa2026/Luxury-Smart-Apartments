"""Read-only inventory advice with synthetic source records and no provider traffic."""

import hashlib
import json
import os
from copy import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.integrations.hostaway.exceptions import HostawayResponseError
from apps.reservations.models import DayUseStay, MorningStay, Reservation
from apps.reservations.services import sama_day_use_availability as source
from tests.test_availability_service import make_day, make_property
from tests.test_guest_classification import factory as _booking_factory

pytestmark = pytest.mark.django_db
booking_factory = _booking_factory
URL = "/reservations/sama/day-use-availability/"
TOKEN = "ab" * 32


@pytest.fixture(autouse=True)
def rig(settings, monkeypatch):
    cache.clear()
    settings.SAMA_BOOKING_ENABLED = settings.DAY_USE08_ENABLED = True
    settings.SAMA_BOOKING_KEY_SHA256 = hashlib.sha256(TOKEN.encode()).hexdigest()
    settings.HYPERPAY_ENVIRONMENT = "production"
    monkeypatch.setattr("httpx.Client.request", Mock(side_effect=AssertionError("no live IO")))
    clock = SimpleNamespace(now=datetime(2026, 11, 28, 1, tzinfo=UTC))
    monkeypatch.setattr(source.timezone, "now", lambda: clock.now)
    remote = MagicMock()
    remote.__enter__.return_value = remote
    remote.guest_stay_page.return_value = []
    remote.get_listing_calendar.side_effect = lambda *args, **kw: SimpleNamespace(
        days=tuple(make_day(kw["start_date"] + timedelta(days=i)) for i in range(3))
    )
    monkeypatch.setattr(source, "HostawayClient", lambda **kwargs: remote)
    yield SimpleNamespace(clock=clock, remote=remote)
    cache.clear()


def property_obj():
    p = make_property()
    p.is_visible, p.hostaway_is_active = True, True
    p.hostaway_listing_map_id = 97008
    p.time_zone_name = "Asia/Riyadh"
    p.save()
    return p


def read():
    return Client(enforce_csrf_checks=True).get(
        URL, secure=True, HTTP_AUTHORIZATION="Bearer " + TOKEN
    )


def test_verified_inventory_is_read_only_and_never_quotes_or_authorizes(rig):
    p = property_obj()
    with CaptureQueriesContext(connection) as queries:
        result = read()
    assert result.status_code == 200
    data = result.json()
    assert data["contract_version"] == source.CONTRACT and data["complete"]
    assert data["offering_allowed"] and data["coverage"] == {
        "total": 1,
        "evaluated": 1,
        "unknown": 0,
    }
    assert data["properties"][0]["property_slug"] == p.slug
    assert data["properties"][0]["eligible"] is True
    assert not data["booking_authorized"] and not data["physical_readiness_verified"]
    assert data["properties"][0]["booking_url"] is None
    assert "no-store" in result["Cache-Control"] and result["Referrer-Policy"] == "no-referrer"
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries)
    rig.remote.get_listing_currency_record.assert_not_called()
    rig.remote.create_reservation_with_price_details.assert_not_called()
    assert "amount_sar" not in str(data)


def test_remote_cache_is_fixed_age_not_sliding_and_local_admissions_are_always_read(
    rig, booking_factory
):
    booking = booking_factory("synthetic@example.invalid", "+16502530000")
    p = booking.property
    p.hostaway_listing_map_id, p.time_zone_name = 97008, "Asia/Riyadh"
    p.is_visible = True
    p.save()
    first = read().json()
    assert first["properties"][0]["eligible"]
    rig.clock.now += timedelta(seconds=5)
    again = read().json()
    assert again["source_checked_at"] == first["source_checked_at"]
    assert again["expires_at"] == first["expires_at"]
    assert rig.remote.get_listing_calendar.call_count == 1
    booking.check_in = rig.clock.now.date() - timedelta(days=3)
    booking.check_out = rig.clock.now.date()
    booking.save()
    blocked = read().json()
    assert blocked["properties"][0]["eligible"] is False
    assert blocked["properties"][0]["reason_codes"] == ["local_stay_or_departure"]
    assert rig.remote.get_listing_calendar.call_count == 1


def test_expired_remote_evidence_is_reloaded_and_new_ota_block_is_not_ignored(rig):
    property_obj()
    first = read().json()
    rig.clock.now += timedelta(seconds=15)
    rig.remote.get_listing_calendar.side_effect = lambda *args, **kw: SimpleNamespace(
        days=tuple(
            replace(make_day(kw["start_date"] + timedelta(days=i)), is_available=False)
            for i in range(3)
        )
    )
    second = read().json()
    assert first["properties"][0]["eligible"] and second["properties"][0]["eligible"] is False
    assert second["source_checked_at"] != first["source_checked_at"]
    assert second["source_revision"] != first["source_revision"]
    assert rig.remote.get_listing_calendar.call_count == 2


def test_departing_today_and_long_stays_block_even_when_the_calendar_looks_free(rig):
    p = property_obj()
    rig.remote.guest_stay_page.return_value = [
        {
            "id": 1001,
            "listingMapId": p.hostaway_listing_map_id,
            "status": "modified",
            "arrivalDate": "2026-10-01",
            "departureDate": "2026-11-28",
            "guestName": "never cache me",
            "phone": "never cache me",
            "guestEmail": "never cache me",
        }
    ]
    response = read()
    assert response.status_code == 200
    row = response.json()["properties"][0]
    assert row["eligible"] is False and row["reason_codes"] == ["channel_stay_or_departure"]
    assert "never cache me" not in str(response.json())
    # Inspect the actual cache writer rather than trusting the response allowlist alone.
    assert "never cache me" not in str(cache._cache)


@pytest.mark.parametrize(
    "kind",
    ["missing_day", "unknown_availability", "unknown_status", "provider_failure", "page_limit"],
)
def test_unverified_source_is_never_a_verified_empty_list(rig, kind):
    p = property_obj()
    if kind in {"missing_day", "unknown_availability"}:
        rig.remote.get_listing_calendar.side_effect = lambda *args, **kw: SimpleNamespace(
            days=tuple(
                replace(
                    make_day(kw["start_date"] + timedelta(days=i)),
                    is_available=None if kind == "unknown_availability" else True,
                )
                for i in range(1 if kind == "missing_day" else 3)
            )
        )
    elif kind == "provider_failure":
        rig.remote.get_listing_calendar.side_effect = HostawayResponseError("private-body")
    elif kind == "unknown_status":
        rig.remote.guest_stay_page.return_value = [
            {
                "id": 1001,
                "listingMapId": p.hostaway_listing_map_id,
                "status": "not-understood",
                "arrivalDate": "2026-11-27",
                "departureDate": "2026-11-29",
            }
        ]
    else:
        rig.remote.guest_stay_page.side_effect = lambda *args, **kw: [
            {"id": (kw.get("after_id") or 0) + i + 1, "listingMapId": 99999, "status": "new"}
            for i in range(100)
        ]
    response = read()
    assert response.status_code == 503
    data = response.json()
    assert not data["complete"] and not data["offering_allowed"]
    assert data["properties"][0]["eligible"] is None and data["coverage"]["unknown"] == 1
    assert "private-body" not in str(data)


def test_complete_occupancy_pagination_is_read_even_for_other_listings(rig):
    property_obj()
    rig.remote.guest_stay_page.side_effect = (
        lambda *args, **kw: []
        if kw.get("after_id")
        else [{"id": i + 1, "listingMapId": 99999, "status": "new"} for i in range(100)]
    )
    assert read().json()["properties"][0]["eligible"] is True
    assert rig.remote.guest_stay_page.call_count == 2


def test_property_local_midnight_expires_evidence_and_changes_the_inventory_day(rig):
    p = property_obj()
    p.country_code, p.time_zone_name = "MA", "Africa/Casablanca"
    p.save()
    rig.clock.now = datetime(2026, 11, 27, 23, 59, 55, tzinfo=UTC)
    before = read().json()
    assert before["local_date"] == "2026-11-28"
    assert before["properties"][0]["local_date"] == "2026-11-27"
    assert datetime.fromisoformat(before["expires_at"]) == datetime(2026, 11, 28, tzinfo=UTC)
    rig.clock.now += timedelta(seconds=6)
    after = read().json()
    assert after["properties"][0]["local_date"] == "2026-11-28"
    assert after["source_revision"] != before["source_revision"]
    assert rig.remote.get_listing_calendar.call_count == 2


def test_noon_invalidates_the_list_without_a_provider_read(rig):
    property_obj()
    rig.clock.now = datetime(2026, 11, 28, 9, tzinfo=UTC)
    response = read()
    assert response.status_code == 200
    assert response.json()["status"] == "window_closed"
    assert not response.json()["offering_allowed"]
    rig.remote.get_listing_calendar.assert_not_called()


def test_inactive_properties_remain_visible_as_excluded_metadata(rig):
    p = property_obj()
    p.is_visible = False
    p.save()
    result = read().json()
    assert result["complete"] and result["properties"][0]["eligible"] is False
    assert result["properties"][0]["reason_codes"] == ["property_unavailable"]


def test_existing_service_auth_and_no_caller_dates_or_guest_parameters(rig, settings):
    property_obj()
    assert Client().get(URL, secure=True).status_code == 401
    assert Client().get(URL, HTTP_AUTHORIZATION="Bearer " + TOKEN).status_code == 403
    assert Client().post(URL, secure=True, HTTP_AUTHORIZATION="Bearer " + TOKEN).status_code == 405
    assert (
        Client()
        .get(URL + "?date=2026-11-28", secure=True, HTTP_AUTHORIZATION="Bearer " + TOKEN)
        .status_code
        == 400
    )
    settings.DAY_USE08_ENABLED = False
    response = read()
    assert response.status_code == 503 and not response.json()["offering_allowed"]
    rig.remote.get_listing_calendar.assert_not_called()


def test_missing_catalog_is_unknown_not_empty(rig):
    assert read().status_code == 503


@pytest.mark.parametrize("field", ["id", "listingMapId", "status", "arrivalDate", "departureDate"])
def test_invalid_field_shapes_never_put_a_provider_body_in_the_inventory_cache(rig, field):
    p = property_obj()
    row = {
        "id": 1001,
        "listingMapId": p.hostaway_listing_map_id,
        "status": "new",
        "arrivalDate": "2026-11-27",
        "departureDate": "2026-11-29",
    }
    row[field] = {"phone": "private-provider-body"}
    rig.remote.guest_stay_page.return_value = [row]
    response = read()
    assert response.status_code == 503
    assert "private-provider-body" not in str(cache._cache)


@pytest.mark.parametrize(
    "model,key", [(DayUseStay, "hostaway_day_use"), (MorningStay, "hostaway_morning")]
)
@pytest.mark.parametrize("state", ["creating", "cancel_unknown"])
def test_uncertain_same_day_writes_block_even_with_empty_remote_calendar(rig, model, key, state):
    p = property_obj()
    now = rig.clock.now
    inventory = (
        {
            "inventory_arrival_date": now.date() - timedelta(days=1),
            "inventory_departure_date": now.date() + timedelta(days=1),
        }
        if model is DayUseStay
        else {}
    )
    model.objects.create(
        request_id=uuid4(),
        property=p,
        starts_at=now,
        ends_at=now + timedelta(hours=3),
        expires_at=now - timedelta(seconds=1),
        availability_checked_at=now,
        created_by=get_user_model().objects.create_user(username="synthetic-admin"),
        guest_name="Synthetic",
        guest_phone="+16502530000",
        guest_email="synthetic@example.invalid",
        amount_sar=100,
        environment="production",
        status="cancelled",
        availability_evidence={key: {"state": state}},
        **inventory,
    )
    response = read()
    assert response.status_code == 200 and response.json()["properties"][0]["eligible"] is False
    rig.remote.get_listing_calendar.assert_not_called()


def test_booking_admitted_for_an_earlier_property_during_a_later_read_is_excluded(
    rig, booking_factory
):
    booking = booking_factory("synthetic@example.invalid", "+16502530000")
    p = booking.property
    p.is_visible, p.time_zone_name, p.hostaway_listing_map_id = True, "Asia/Riyadh", 97008
    p.save()
    another = copy(p)
    another.pk = None
    another.hostaway_listing_id += 1
    another.hostaway_listing_map_id += 1
    another.slug += "-second"
    another.save()

    def calendar(listing_id, **kw):
        if listing_id == another.hostaway_listing_id:
            Reservation.objects.filter(pk=booking.pk).update(
                check_in=rig.clock.now.date() - timedelta(days=2), check_out=rig.clock.now.date()
            )
        return SimpleNamespace(
            days=tuple(make_day(kw["start_date"] + timedelta(days=i)) for i in range(3))
        )

    rig.remote.get_listing_calendar.side_effect = calendar
    data = read().json()
    assert (
        next(row for row in data["properties"] if row["property_slug"] == p.slug)["eligible"]
        is False
    )


def test_changed_mapping_during_remote_read_is_unknown(rig):
    p = property_obj()

    def calendar(*args, **kw):
        type(p).objects.filter(pk=p.pk).update(is_visible=False)
        return SimpleNamespace(
            days=tuple(make_day(kw["start_date"] + timedelta(days=i)) for i in range(3))
        )

    rig.remote.get_listing_calendar.side_effect = calendar
    response = read()
    assert response.status_code == 503
    assert response.json()["properties"][0]["reason_codes"] == ["source_metadata_changed"]


def test_deadline_and_evidence_expiry_fail_closed(rig, monkeypatch):
    property_obj()
    times = iter([0, 0, 20])
    monkeypatch.setattr(source, "clock", SimpleNamespace(monotonic=lambda: next(times, 20)))
    response = read()
    assert response.status_code == 503
    assert response.json().get("offering_allowed", False) is False


def test_source_evidence_is_not_relabelled_fresh_when_a_slow_read_crosses_noon(rig):
    property_obj()
    rig.clock.now = datetime(2026, 11, 28, 8, 59, 59, tzinfo=UTC)

    def calendar(*args, **kw):
        rig.clock.now += timedelta(seconds=2)
        return SimpleNamespace(
            days=tuple(make_day(kw["start_date"] + timedelta(days=i)) for i in range(3))
        )

    rig.remote.get_listing_calendar.side_effect = calendar
    assert read().status_code == 503


def test_export_exact_six_property_source_body(rig, tmp_path):
    initial = property_obj()
    for i in range(1, 6):
        p = copy(initial)
        p.pk = None
        p.hostaway_listing_id += i
        p.hostaway_listing_map_id += i
        p.slug = f"synthetic-unit-{i}"
        p.name_ar, p.name_en, p.name_fr = (
            f"وحدة تجريبية {i}",
            f"Synthetic Unit {i}",
            f"Unité fictive {i}",
        )
        p.time_zone_name = "Africa/Casablanca" if i == 1 else "Asia/Riyadh"
        p.country_code = "MA" if i == 1 else "SA"
        p.is_visible = i != 5
        p.save()
    response = read()
    assert response.status_code == 200 and response.json()["coverage"]["total"] == 6
    output = Path(os.environ.get("LSA_SOURCE_FIXTURE_DIR", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    (output / "source-body.synthetic.json").write_text(
        json.dumps(response.json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
