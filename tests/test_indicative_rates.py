"""The "from" price anchor: how it is computed, and what it refuses to do."""

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import Client

from apps.integrations.hostaway.availability_validators import CalendarDay, CalendarDocument
from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import Property

pytestmark = pytest.mark.django_db


def make_property(listing_id: int = 9101, **overrides: object) -> Property:
    defaults = {
        "hostaway_listing_id": listing_id,
        "slug": f"anchor-{listing_id}",
        "hostaway_name": "Source",
        "name_ar": "وحدة",
        "name_en": "Unit",
        "city": "Riyadh",
        "city_ar": "الرياض",
        "country_code": "SA",
        "currency_code": "SAR",
        "is_visible": True,
    }
    return Property.objects.create(**{**defaults, **overrides})


def calendar(*days: tuple[bool | None, str | None]) -> CalendarDocument:
    return CalendarDocument(
        days=tuple(
            CalendarDay(
                date=date(2026, 9, 4) + timedelta(days=offset),
                is_available=available,
                price=Decimal(price) if price is not None else None,
                minimum_stay=None,
                maximum_stay=None,
                closed_on_arrival=None,
                closed_on_departure=None,
                status="available" if available else "unavailable",
            )
            for offset, (available, price) in enumerate(days)
        ),
        envelope_fields=frozenset(),
        day_field_types=(),
    )


def run(**options: object) -> str:
    out = StringIO()
    call_command("refresh_indicative_rates", stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


def with_calendar(document: object) -> object:
    client = patch("apps.properties.management.commands.refresh_indicative_rates.HostawayClient")
    mock = client.start()
    instance = mock.return_value.__enter__.return_value
    if isinstance(document, Exception):
        instance.get_listing_calendar.side_effect = document
    else:
        instance.get_listing_calendar.return_value = document
    return client


def test_the_anchor_is_the_cheapest_available_night() -> None:
    property_obj = make_property()
    patcher = with_calendar(calendar((True, "820"), (True, "590"), (True, "740")))

    try:
        run()
    finally:
        patcher.stop()

    property_obj.refresh_from_db()
    assert property_obj.indicative_nightly_from == Decimal("590.00")
    assert property_obj.indicative_currency == "SAR"


def test_an_unavailable_night_never_sets_the_anchor() -> None:
    property_obj = make_property()
    patcher = with_calendar(calendar((False, "100"), (True, "700")))

    try:
        run()
    finally:
        patcher.stop()

    property_obj.refresh_from_db()
    assert property_obj.indicative_nightly_from == Decimal("700.00")


def test_unknown_availability_is_not_treated_as_bookable() -> None:
    property_obj = make_property()
    patcher = with_calendar(calendar((None, "100"), (True, "650")))

    try:
        run()
    finally:
        patcher.stop()

    property_obj.refresh_from_db()
    assert property_obj.indicative_nightly_from == Decimal("650.00")


def test_a_window_with_no_availability_leaves_the_anchor_alone() -> None:
    property_obj = make_property(indicative_nightly_from=Decimal("500.00"))
    patcher = with_calendar(calendar((False, "100"), (False, "200")))

    try:
        output = run()
    finally:
        patcher.stop()

    property_obj.refresh_from_db()
    assert property_obj.indicative_nightly_from == Decimal("500.00")
    assert "no_availability=1" in output


def test_a_failed_connection_writes_nothing() -> None:
    """A network problem must never blank an anchor already on the site."""
    property_obj = make_property(indicative_nightly_from=Decimal("500.00"))
    patcher = with_calendar(HostawayError("unreachable"))

    try:
        output = run()
    finally:
        patcher.stop()

    property_obj.refresh_from_db()
    assert property_obj.indicative_nightly_from == Decimal("500.00")
    assert "failed=1" in output


def test_dry_run_reports_without_writing() -> None:
    property_obj = make_property()
    patcher = with_calendar(calendar((True, "590")))

    try:
        output = run(dry_run=True)
    finally:
        patcher.stop()

    property_obj.refresh_from_db()
    assert property_obj.indicative_nightly_from is None
    assert "(dry run)" in output
    assert "updated=1" in output


def test_an_archived_property_is_skipped() -> None:
    # hostaway_is_active is derived from the special status, per the README.
    make_property(hostaway_special_status="archived", hostaway_is_active=False)
    patcher = with_calendar(calendar((True, "590")))

    try:
        output = run()
    finally:
        patcher.stop()

    assert "examined=0" in output


# --- what the pages show ----------------------------------------------------


def test_the_card_shows_the_anchor_as_a_from_price() -> None:
    make_property(indicative_nightly_from=Decimal("590.00"), indicative_currency="SAR")

    content = Client().get("/properties/").content.decode()

    assert "٥٩٠" in content
    assert "Live price checks" not in content


def test_the_card_keeps_the_original_wording_without_an_anchor() -> None:
    make_property()

    content = Client().get("/properties/").content.decode()

    # The Arabic translation of "Price based on dates" is what ships; assert the
    # anchor markup is simply absent.
    assert "/ night" not in content
    assert "/ ليلة" not in content


def test_the_property_page_shows_the_anchor() -> None:
    property_obj = make_property(
        indicative_nightly_from=Decimal("590.00"),
        indicative_currency="SAR",
    )

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "booking-sidebar__anchor" in content
    assert "٥٩٠" in content
