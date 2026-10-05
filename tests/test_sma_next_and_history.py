from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.test import Client

from apps.integrations.hostaway.availability_validators import CalendarDay
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayResponseError
from apps.reservations import sama_booking_api as api
from apps.reservations.models import Reservation
from apps.reservations.services import sama_guest_history as history
from apps.reservations.services import sama_next_availability as next_service
from tests import test_sama_guest_context as fixtures
from tests.test_sama_booking_api import ENABLED, HEADERS, post
from tests.test_sama_guest_context import PHONE

pytestmark = pytest.mark.django_db


@pytest.fixture
def context(monkeypatch):
    yield from fixtures.context.__wrapped__(monkeypatch)


def calendar_factory(free_from, **changes):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)

    def calendar(_id, *, start_date, end_date, include_resources):
        assert include_resources is False
        return SimpleNamespace(
            days=tuple(
                CalendarDay(
                    date=start_date + timedelta(days=i),
                    is_available=start_date + timedelta(days=i) >= free_from,
                    price=None,
                    minimum_stay=1,
                    maximum_stay=366,
                    closed_on_arrival=False,
                    closed_on_departure=False,
                    status="",
                    **changes,
                )
                for i in range((end_date - start_date).days + 1)
            )
        )

    client.get_listing_calendar.side_effect = calendar
    return client, Mock(return_value=client)


def test_calendar_provider_with_short_window_projection_keeps_long_horizon(context):
    p, _, _, _ = context
    client, factory = calendar_factory(date(2027, 5, 1))
    original = client.get_listing_calendar.side_effect

    def short_response(*args, **kwargs):
        document = original(*args, **kwargs)
        return SimpleNamespace(days=document.days[:91])

    client.get_listing_calendar.side_effect = short_response
    result = next_service.next_availability(
        p, "2026-10-10", "2026-10-13", 2, client_factory=factory
    )
    assert result["next_stay"]["check_in"] == "2027-05-01"
    assert all(
        (c.kwargs["end_date"] - c.kwargs["start_date"]).days <= 90
        for c in client.get_listing_calendar.call_args_list
    )


def test_long_booking_next_whole_stay_not_ninety_day_cutoff(context):
    p, _, _, _ = context
    free = date(2027, 5, 1)
    client, factory = calendar_factory(free)
    result = next_service.next_availability(
        p, "2026-10-10", "2026-10-13", 2, client_factory=factory
    )
    assert result["next_stay"] == {"check_in": "2027-05-01", "check_out": "2027-05-04", "nights": 3}
    assert result["requested_reason"] == "unavailable_dates"
    assert client.get_listing_calendar.call_count == 3
    assert Reservation.objects.count() == 0


def test_no_dates_or_count_uses_local_today_and_does_not_claim_price(context):
    p, _, _, _ = context
    _, factory = calendar_factory(date(2026, 10, 6))
    result = next_service.next_availability(p, None, None, None, client_factory=factory)
    assert result["next_stay"]["check_in"] == "2026-10-06"
    assert "price" not in str(result) and result["request"]["guests"] is None


def test_blocked_night_inside_stay_makes_next_start_shift(context):
    p, _, _, _ = context
    client, factory = calendar_factory(date(2026, 10, 4))
    original = client.get_listing_calendar.side_effect
    from dataclasses import replace

    def blocked(*args, **kwargs):
        doc = original(*args, **kwargs)
        return SimpleNamespace(
            days=tuple(
                replace(d, is_available=False) if d.date == date(2026, 10, 7) else d
                for d in doc.days
            )
        )

    client.get_listing_calendar.side_effect = blocked
    result = next_service.next_availability(
        p, "2026-10-06", "2026-10-09", 2, client_factory=factory
    )
    assert result["next_stay"]["check_in"] == "2026-10-08"


def test_minimum_stay_arrival_departure_and_conflicting_inventory(context):
    from dataclasses import replace

    p, _, _, _ = context
    client, factory = calendar_factory(date(2026, 10, 4))
    original = client.get_listing_calendar.side_effect

    def restricted(*args, **kwargs):
        doc = original(*args, **kwargs)
        return SimpleNamespace(
            days=tuple(
                replace(d, minimum_stay=5)
                if d.date == date(2026, 10, 6)
                else replace(d, closed_on_arrival=True)
                if d.date == date(2026, 10, 7)
                else replace(d, available_units_to_sell=0)
                if d.date == date(2026, 10, 8)
                else replace(d, closed_on_departure=True)
                if d.date == date(2026, 10, 11)
                else d
                for d in doc.days
            )
        )

    client.get_listing_calendar.side_effect = restricted
    result = next_service.next_availability(
        p, "2026-10-06", "2026-10-08", 2, client_factory=factory
    )
    assert result["next_stay"]["check_in"] == "2026-10-10"
    assert result["requested_reason"] == "minimum_stay_not_met"


def test_calendar_windows_are_complete_bounded_and_cross_boundary(context):
    p, _, _, _ = context
    free = date(2027, 11, 1)
    client, factory = calendar_factory(free)
    result = next_service.next_availability(
        p, "2026-10-10", "2026-10-13", 2, client_factory=factory
    )
    assert result["next_stay"]["check_in"] == free.isoformat()
    assert client.get_listing_calendar.call_count == 5
    for call in client.get_listing_calendar.call_args_list:
        assert (call.kwargs["end_date"] - call.kwargs["start_date"]).days <= 366
    cache.clear()
    client.get_listing_calendar.side_effect = lambda *a, **k: SimpleNamespace(days=())
    with pytest.raises(HostawayResponseError):
        next_service.next_availability(p, "2026-10-10", "2026-10-13", 2, client_factory=factory)


@pytest.mark.parametrize(
    "dates", [("2026-10-03", None), (None, "2026-10-05"), ("2026-10-06", "2026-10-06")]
)
def test_bad_dates_do_not_query_provider(context, dates):
    p, _, _, _ = context
    client, factory = calendar_factory(date(2026, 10, 4))
    with pytest.raises(ValueError):
        next_service.next_availability(p, *dates, None, client_factory=factory)
    client.get_listing_calendar.assert_not_called()


def test_history_matches_complete_phone_sorts_previous_stays_and_has_no_private_details(context):
    p, row, client, factory = context
    past = {**row, "arrivalDate": "2026-08-01", "departureDate": "2026-08-08", "numberOfGuests": 4}
    client.guest_history_page.side_effect = [
        [
            past,
            {**past, "id": 112, "phone": "+966500000008"},
            {**past, "id": 113, "status": "cancelled"},
            {**row, "id": 114},
        ],
        [],
    ]
    result = history.guest_history(PHONE, client_factory=factory)
    assert result["coverage"] == "complete" and len(result["stays"]) == 1
    assert result["stays"][0]["property_slug"] == p.slug and result["stays"][0]["guests"] == 4
    assert PHONE not in str(result) and "NEVER_KEEP" not in str(result)
    assert Reservation.objects.count() == 0
    assert history.guest_history("+966500000008", client_factory=factory)["stays"]
    assert client.guest_history_page.call_count == 2
    assert "NEVER_KEEP" not in str(cache.get("sma-guest-history-v1:2026-10-04"))


def test_history_partial_scan_is_labelled_and_resumes_without_claiming_latest(context, monkeypatch):
    p, row, client, factory = context
    old = {**row, "arrivalDate": "2026-08-01", "departureDate": "2026-08-02"}
    client.guest_history_page.side_effect = [[{**old, "id": i} for i in range(1, 101)], []]
    ticks = iter([0, 0, 0, 19, 20, 20, 20, 20])
    monkeypatch.setattr(history.time, "monotonic", lambda: next(ticks))
    first = history.guest_history(PHONE, client_factory=factory)
    assert first["coverage"] == "partial"
    second = history.guest_history(PHONE, client_factory=factory)
    assert second["coverage"] == "complete"
    assert client.guest_history_page.call_args.kwargs["after_id"] == 100


def test_history_repeated_pages_fail_closed(context):
    p, row, client, factory = context
    client.guest_history_page.return_value = [{**row, "id": i} for i in range(1, 101)]
    with pytest.raises(HostawayResponseError):
        history.guest_history(PHONE, client_factory=factory)


def test_flexible_duration_keeps_earliest_arrival_when_first_checkout_is_closed(context):
    from dataclasses import replace

    p, _, _, _ = context
    client, factory = calendar_factory(date(2026, 10, 4))
    original = client.get_listing_calendar.side_effect

    def restricted(*args, **kwargs):
        doc = original(*args, **kwargs)
        return SimpleNamespace(
            days=tuple(
                replace(d, closed_on_departure=True) if d.date == date(2026, 10, 5) else d
                for d in doc.days
            )
        )

    client.get_listing_calendar.side_effect = restricted
    result = next_service.next_availability(p, None, None, None, client_factory=factory)
    assert result["next_stay"] == {"check_in": "2026-10-04", "check_out": "2026-10-06", "nights": 2}


def test_earlier_long_minimum_is_resolved_before_later_short_stay(context):
    from dataclasses import replace

    p, _, _, _ = context
    client, factory = calendar_factory(date(2026, 10, 4))
    original = client.get_listing_calendar.side_effect

    def restricted(*args, **kwargs):
        doc = original(*args, **kwargs)
        return SimpleNamespace(
            days=tuple(
                replace(d, minimum_stay=366) if d.date == date(2026, 10, 4) else d for d in doc.days
            )
        )

    client.get_listing_calendar.side_effect = restricted
    result = next_service.next_availability(p, None, None, None, client_factory=factory)
    assert result["next_stay"]["check_in"] == "2026-10-04"
    assert result["next_stay"]["nights"] == 366 and client.get_listing_calendar.call_count == 5


def test_next_calendar_cannot_return_success_after_deadline(context, monkeypatch):
    p, _, _, _ = context
    _, factory = calendar_factory(date(2026, 10, 4))
    ticks = iter([0, 0, 0, 23])
    monkeypatch.setattr(next_service.time, "monotonic", lambda: next(ticks))
    with pytest.raises(HostawayResponseError, match="time bound"):
        next_service.next_availability(p, None, None, None, client_factory=factory)


def test_history_short_nonterminal_pages_are_not_assumed_complete(context):
    _, row, client, factory = context
    old = {**row, "arrivalDate": "2026-08-01", "departureDate": "2026-08-02"}
    client.guest_history_page.side_effect = [
        [old],
        [{**old, "id": 1, "departureDate": "2026-08-05"}],
        [],
    ]
    result = history.guest_history(PHONE, client_factory=factory)
    assert result["coverage"] == "complete" and len(result["stays"]) == 2
    assert result["stays"][0]["check_out"] == "2026-08-05"
    assert client.guest_history_page.call_count == 3
    assert client.guest_history_page.call_args_list[1].kwargs["after_id"] == 111


def test_history_competing_request_does_not_start_second_scan(context):
    _, _, client, factory = context
    cache.add("sma-guest-history-v1:2026-10-04:lock", "another-owner", timeout=30)
    result = history.guest_history(PHONE, client_factory=factory)
    assert result["coverage"] == "partial" and result["stays"] == []
    client.guest_history_page.assert_not_called()


def test_history_rejects_oversized_page_before_projection(context):
    _, row, client, factory = context
    client.guest_history_page.return_value = [{**row, "id": i} for i in range(1, 102)]
    with pytest.raises(HostawayResponseError, match="Unbounded"):
        history.guest_history(PHONE, client_factory=factory)


def test_history_hard_cap_stays_partial_and_does_not_resume_unbounded(context):
    _, _, client, factory = context
    cache.set(
        "sma-guest-history-v1:2026-10-04",
        {
            "stays": [],
            "seen": list(range(1, 10001)),
            "cursor": 10000,
            "complete": False,
            "bounded": True,
            "checked_at": "2026-10-04T09:00:00+00:00",
        },
    )
    assert history.guest_history(PHONE, client_factory=factory)["coverage"] == "partial"
    client.guest_history_page.assert_not_called()


def test_history_client_uses_documented_read_filters_without_a_phone():
    client = object.__new__(HostawayClient)
    client._get_json = Mock(return_value={"status": "success", "result": []})
    client.guest_history_page(date(2026, 10, 4), after_id=111)
    assert client._get_json.call_args.args == ("/reservations",)
    assert dict(client._get_json.call_args.kwargs["params"]) == {
        "limit": 100,
        "includeResources": 0,
        "departureEndDate": "2026-10-04",
        "afterId": 111,
    }


@ENABLED
@pytest.mark.parametrize(
    "path,body",
    [
        ("/reservations/sama/guest-history/", {"phone": PHONE}),
        (
            "/reservations/sama/next-availability/",
            {"property_slug": "example", "check_in": None, "check_out": None, "guests": None},
        ),
    ],
)
def test_new_read_endpoints_require_https_and_existing_bearer(path, body):
    assert (
        Client().post(path, body, content_type="application/json", secure=True).status_code == 401
    )
    assert Client().post(path, body, content_type="application/json", **HEADERS).status_code == 403
    assert Client().get(path, secure=True, **HEADERS).status_code == 405


@ENABLED
def test_history_endpoint_validates_phone_and_does_not_write(context, monkeypatch):
    fn = Mock(return_value={"code": "guest_history", "stays": []})
    monkeypatch.setattr(api, "read_guest_history", fn)
    for phone in (None, True, 123, [], {}, "", "0500000009", "+966 50 000 0009", "bad"):
        assert post("/reservations/sama/guest-history/", {"phone": phone}).status_code == 400
    fn.assert_not_called()
    assert post("/reservations/sama/guest-history/", {"phone": PHONE}).status_code == 200


@ENABLED
def test_next_endpoint_and_failure_do_not_expose_provider_body(context, monkeypatch):
    p, _, _, _ = context
    fn = Mock(return_value={"code": "next_availability", "next_stay": None})
    monkeypatch.setattr(api, "read_next_availability", fn)
    body = {"property_slug": p.slug, "check_in": None, "check_out": None, "guests": None}
    result = post("/reservations/sama/next-availability/", body)
    assert result.status_code == 200 and result["Cache-Control"] == "no-store"
    fn.side_effect = TimeoutError("SECRET-PROVIDER-BODY")
    result = post("/reservations/sama/next-availability/", body)
    assert result.status_code == 503 and "SECRET" not in result.content.decode()
