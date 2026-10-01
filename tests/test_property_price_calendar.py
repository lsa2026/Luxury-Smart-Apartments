from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import PropertyPriceCalendar
from apps.properties.price_calendar import PropertyPriceCalendarView
from tests.test_indicative_rates import calendar, make_property, run, with_calendar

pytestmark = pytest.mark.django_db


def snapshot(property_obj, *, age=0):
    today = timezone.localdate()
    return PropertyPriceCalendar.objects.create(
        property=property_obj,
        currency="SAR",
        start_date=today,
        end_date=today + timedelta(days=365),
        fetched_at=timezone.now() - timedelta(hours=age),
        days=[
            {
                "date": (today + timedelta(days=index)).isoformat(),
                "price": str(665 + index * 25),
                "available": index != 2,
                "arrival_available": index != 2,
                "departure_available": True,
                "minimum_stay": 1,
            }
            for index in range(65)
        ],
    )


def endpoint(property_obj, *, days=62):
    today = timezone.localdate()
    url = reverse("properties:price_calendar", kwargs={"slug": property_obj.slug})
    return f"{url}?start={today.isoformat()}&end={(today + timedelta(days=days)).isoformat()}"


def test_refresh_stores_only_public_calendar_facts():
    property_obj = make_property()
    patcher = with_calendar(calendar((True, "665"), (False, "100"), (None, None)))
    try:
        run()
    finally:
        patcher.stop()
    saved = PropertyPriceCalendar.objects.get(property=property_obj)
    assert saved.currency == "SAR"
    assert saved.days[0]["price"] == "665"
    assert saved.days[1]["available"] is False
    assert saved.days[2]["available"] is False
    assert set(saved.days[0]) == {
        "date",
        "price",
        "available",
        "arrival_available",
        "departure_available",
        "minimum_stay",
    }
    assert saved.end_date - saved.start_date == timedelta(days=365)


def test_unchanged_price_still_refreshes_verification_timestamp():
    old = timezone.now() - timedelta(days=4)
    property_obj = make_property(
        indicative_nightly_from=Decimal("665"),
        indicative_currency="SAR",
        indicative_priced_at=old,
    )
    saved = snapshot(property_obj, age=96)
    patcher = with_calendar(calendar((True, "665")))
    try:
        run()
    finally:
        patcher.stop()
    saved.refresh_from_db()
    property_obj.refresh_from_db()
    assert saved.fetched_at > old
    assert property_obj.indicative_priced_at == saved.fetched_at


def test_failed_daily_refresh_preserves_snapshot_and_timestamp():
    property_obj = make_property()
    saved = snapshot(property_obj)
    previous = saved.fetched_at
    patcher = with_calendar(HostawayError("offline"))
    try:
        run()
    finally:
        patcher.stop()
    saved.refresh_from_db()
    assert saved.fetched_at == previous


def test_empty_availability_still_publishes_an_unavailable_calendar():
    property_obj = make_property()
    patcher = with_calendar(calendar((False, "665")))
    try:
        run()
    finally:
        patcher.stop()
    assert not PropertyPriceCalendar.objects.get(property=property_obj).days[0]["available"]


def test_dry_run_does_not_write_snapshots():
    make_property()
    patcher = with_calendar(calendar((True, "665")))
    try:
        run(dry_run=True)
    finally:
        patcher.stop()
    assert not PropertyPriceCalendar.objects.exists()


def test_only_one_provider_calendar_read_per_listing():
    make_property()
    with patch(
        "apps.properties.management.commands.refresh_indicative_rates.HostawayClient"
    ) as provider:
        api = provider.return_value.__enter__.return_value
        api.get_listing_calendar.return_value = calendar((True, "665"))
        run()
        assert api.get_listing_calendar.call_count == 1
        parameters = api.get_listing_calendar.call_args.kwargs
        assert parameters.get("include_resources", False) is False
        assert parameters["end_date"] - parameters["start_date"] == timedelta(days=365)


def test_release_warmup_reads_only_missing_calendars():
    existing = make_property(9101)
    snapshot(existing)
    missing = make_property(9102)
    with patch(
        "apps.properties.management.commands.refresh_indicative_rates.HostawayClient"
    ) as provider:
        api = provider.return_value.__enter__.return_value
        api.get_listing_calendar.return_value = calendar((True, "665"))
        run(if_missing=True)
        assert api.get_listing_calendar.call_count == 1
        assert api.get_listing_calendar.call_args.args == (missing.hostaway_listing_id,)
    assert PropertyPriceCalendar.objects.count() == 2
    with patch(
        "apps.properties.management.commands.refresh_indicative_rates.HostawayClient",
        side_effect=AssertionError("No provider read on later deployments"),
    ):
        run(if_missing=True)


def test_prices_are_read_locally_with_one_database_query(django_assert_num_queries):
    property_obj = make_property()
    snapshot(property_obj)
    with patch(
        "apps.integrations.hostaway.client.HostawayClient",
        side_effect=AssertionError("No live calls"),
    ):
        with django_assert_num_queries(1):
            response = PropertyPriceCalendarView.as_view()(
                RequestFactory().get(endpoint(property_obj)), slug=property_obj.slug
            )
    import json

    data = json.loads(response.content)
    assert response.status_code == 200
    assert len(data["days"]) == 62
    assert data["days"][0]["price"] == "665"
    assert data["currency"] == "SAR"
    assert data["updated_at"]
    assert response["Cache-Control"] == "private, max-age=300"


def test_stale_calendar_does_not_return_old_prices():
    property_obj = make_property()
    snapshot(property_obj, age=49)
    response = Client().get(endpoint(property_obj))
    assert response.status_code == 503
    assert "days" not in response.json()
    assert response.json()["detail"] == "snapshot_outdated"


@pytest.mark.parametrize("visible,active", [(False, True), (True, False)])
def test_hidden_and_archived_listings_are_not_exposed(visible, active):
    property_obj = make_property(is_visible=visible)
    if not active:
        property_obj.hostaway_special_status = "archived"
        property_obj.save()
    snapshot(property_obj)
    assert Client().get(endpoint(property_obj)).status_code == 404


@pytest.mark.parametrize("query", ["", "?start=bad&end=bad", "?start=2020-01-01&end=2030-01-01"])
def test_unbounded_or_invalid_requests_are_rejected(query):
    property_obj = make_property()
    url = reverse("properties:price_calendar", kwargs={"slug": property_obj.slug})
    assert Client().get(url + query).status_code == 400


def test_calendar_is_get_only():
    property_obj = make_property()
    assert Client().post(endpoint(property_obj)).status_code == 405


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_price_and_rating_links_preserve_property_and_language(language):
    property_obj = make_property(
        315815,
        trustindex_rating=Decimal("4.7"),
        trustindex_review_count=117,
        indicative_nightly_from=665,
        indicative_currency="SAR",
    )
    content = Client().get(f"/{language}/properties/").content.decode()
    assert f"/{language}/properties/{property_obj.slug}/reviews/" in content
    assert f"/{language}/properties/{property_obj.slug}/price-calendar/" in content
    assert "data-price-calendar-open" in content
    # Assets are loaded on interaction, never an eager script request.
    assert 'src="/static/js/price-calendar.js' not in content
    assert 'href="/static/css/price-calendar.css' not in content
    assert content.count('<dialog class="price-calendar"') == 1
    expected = {
        "ar": ('data-available="متاح"', 'data-updated="آخر تحديث"'),
        "en": ('data-available="Available"', 'data-updated="Last updated"'),
        "fr": ('data-available="Disponible"', 'data-updated="Dernière mise à jour"'),
    }
    for label in expected[language]:
        assert label in content


def test_calendar_does_not_add_queries_to_property_listing(django_assert_num_queries):
    property_obj = make_property()
    snapshot(property_obj)
    from apps.properties.views import PropertyListView

    view = PropertyListView()
    view.request = RequestFactory().get("/en/properties/")
    with django_assert_num_queries(2):
        rows = list(view.get_queryset())
        assert len(rows) == 1
    assert "price_calendar" not in rows[0]._state.fields_cache
