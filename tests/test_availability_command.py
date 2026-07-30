from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

from apps.integrations.hostaway.availability_validators import (
    CalendarDay,
    CalendarDocument,
    PriceQuote,
)
from apps.integrations.hostaway.client import HostawayClient
from apps.properties.models import Property, PropertyImage
from apps.reviews.models import Review

pytestmark = pytest.mark.django_db


def make_property() -> Property:
    return Property.objects.create(
        hostaway_listing_id=315816,
        slug="verified-e12",
        name_ar="شقة E12",
        hostaway_name="Synthetic E12",
        person_capacity=4,
        currency_code="SAR",
        is_visible=True,
    )


def calendar_document(start: object, days: int) -> CalendarDocument:
    return CalendarDocument(
        days=tuple(
            CalendarDay(
                date=start + timedelta(days=offset),
                is_available=True,
                price=Decimal("200"),
                minimum_stay=2,
                maximum_stay=30,
                closed_on_arrival=False,
                closed_on_departure=False,
                status="available",
            )
            for offset in range(days + 1)
        ),
        envelope_fields=frozenset({"status", "result", "count"}),
        day_field_types=(
            ("date", "string"),
            ("isAvailable", "integer"),
            ("price", "string"),
        ),
    )


def quote(check_in: object, check_out: object, guests: int) -> PriceQuote:
    return PriceQuote(
        listing_id=315816,
        check_in=check_in,
        check_out=check_out,
        nights=(check_out - check_in).days,
        guests=guests,
        currency="SAR",
        total_price=Decimal("850.75"),
        components=(),
        calculated_at=timezone.now(),
        envelope_fields=frozenset({"status", "result"}),
        result_field_types=(("totalPrice", "string"),),
        component_field_types=(),
    )


def test_verify_command_does_not_change_database() -> None:
    cache.clear()
    property_obj = make_property()
    start = timezone.localdate() + timedelta(days=5)
    before = (
        Property.objects.count(),
        PropertyImage.objects.count(),
        Review.objects.count(),
    )
    stdout = StringIO()
    with (
        patch.object(
            HostawayClient,
            "get_listing_calendar",
            return_value=calendar_document(start, 2),
        ),
        patch.object(
            HostawayClient,
            "calculate_price",
            return_value=quote(start, start + timedelta(days=2), 2),
        ),
    ):
        call_command(
            "verify_hostaway_availability",
            listing_id=property_obj.hostaway_listing_id,
            check_in=start,
            check_out=start + timedelta(days=2),
            guests=2,
            show_schema=True,
            strict=True,
            stdout=stdout,
        )
    after = (
        Property.objects.count(),
        PropertyImage.objects.count(),
        Review.objects.count(),
    )
    output = stdout.getvalue()
    assert after == before
    assert "Availability: available" in output
    assert "Total price: 850.75" in output
    assert "Reservation created: no" in output
    assert "Authorization" not in output


def test_verify_command_scans_once_and_prices_once() -> None:
    cache.clear()
    property_obj = make_property()
    start = timezone.localdate()
    stdout = StringIO()
    with (
        patch.object(
            HostawayClient,
            "get_listing_calendar",
            return_value=calendar_document(start, 60),
        ) as calendar_mock,
        patch.object(
            HostawayClient,
            "calculate_price",
            return_value=quote(start, start + timedelta(days=2), 2),
        ) as price_mock,
    ):
        call_command(
            "verify_hostaway_availability",
            listing_id=property_obj.hostaway_listing_id,
            scan_days=60,
            stay_nights=2,
            guests=2,
            diagnose_inventory=True,
            stdout=stdout,
        )
    assert calendar_mock.call_count == 1
    assert price_mock.call_count == 1
    assert "Selected stay strategy: is_available" in stdout.getvalue()
    assert "No-availability diagnosis: not_applicable" in stdout.getvalue()


def test_verify_command_inventory_diagnosis_is_aggregate_only() -> None:
    cache.clear()
    property_obj = make_property()
    start = timezone.localdate()
    document = calendar_document(start, 2)
    document = CalendarDocument(
        days=tuple(
            CalendarDay(
                date=day.date,
                is_available=False,
                price=Decimal("0"),
                minimum_stay=1,
                maximum_stay=365,
                closed_on_arrival=False,
                closed_on_departure=False,
                status="blocked",
                available_units_to_sell=0,
                count_reserved_units=1,
                has_reservation_resources=True,
            )
            for day in document.days
        ),
        envelope_fields=document.envelope_fields,
        day_field_types=document.day_field_types,
        has_reservation_resources=True,
    )
    stdout = StringIO()
    with (
        patch.object(
            HostawayClient,
            "get_listing_calendar",
            return_value=document,
        ),
        patch.object(HostawayClient, "calculate_price") as price_mock,
    ):
        call_command(
            "verify_hostaway_availability",
            listing_id=property_obj.hostaway_listing_id,
            scan_days=2,
            stay_nights=2,
            guests=2,
            diagnose_inventory=True,
            bypass_cache=True,
            stdout=stdout,
        )

    output = stdout.getvalue()
    assert "Inventory type: multi_unit" in output
    assert "isAvailable distribution: 0=3 1=0 null=0" in output
    assert "Reservation resources field received: yes" in output
    assert "No-availability diagnosis: no_inventory_to_sell" in output
    assert "reservationId" not in output
    assert price_mock.call_count == 0
