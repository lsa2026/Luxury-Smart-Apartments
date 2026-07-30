from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.integrations.hostaway.availability_validators import (
    CalendarDay,
    CalendarDocument,
    PriceComponent,
    PriceQuote,
)
from apps.integrations.hostaway.exceptions import HostawayTimeoutError
from apps.properties.models import Property, PropertyImage
from apps.reservations.services.availability import (
    AVAILABLE,
    CALENDAR_INCOMPLETE,
    CAPACITY_EXCEEDED,
    CLOSED_ON_ARRIVAL,
    CLOSED_ON_DEPARTURE,
    HOSTAWAY_TEMPORARILY_UNAVAILABLE,
    INVALID_DATES,
    INVENTORY_CONFLICT,
    MAXIMUM_STAY_EXCEEDED,
    MINIMUM_STAY_NOT_MET,
    PAST_CHECK_IN,
    PROPERTY_UNAVAILABLE,
    UNAVAILABLE_DATES,
    AvailabilityRequest,
    AvailabilityService,
    evaluate_calendar,
)
from apps.reviews.models import Review

pytestmark = pytest.mark.django_db


def make_property(
    *,
    visible: bool = True,
    active: bool = True,
    capacity: int = 4,
) -> Property:
    return Property.objects.create(
        hostaway_listing_id=9001,
        slug="availability-property",
        hostaway_name="Synthetic property",
        name_ar="وحدة اختبارية",
        currency_code="SAR",
        person_capacity=capacity,
        is_visible=visible,
        hostaway_special_status="" if active else "archived",
    )


def make_day(day_date: date, **changes: object) -> CalendarDay:
    day = CalendarDay(
        date=day_date,
        is_available=True,
        price=Decimal("100"),
        minimum_stay=1,
        maximum_stay=30,
        closed_on_arrival=False,
        closed_on_departure=False,
        status="available",
    )
    return replace(day, **changes)


def make_calendar(start: date, nights: int = 2) -> CalendarDocument:
    return CalendarDocument(
        days=tuple(make_day(start + timedelta(days=offset)) for offset in range(nights + 1)),
        envelope_fields=frozenset({"status", "result"}),
        day_field_types=(("date", "string"),),
    )


def make_quote(
    *,
    check_in: date,
    check_out: date,
    guests: int = 2,
    currency: str = "SAR",
) -> PriceQuote:
    component = PriceComponent(
        listing_fee_setting_id=None,
        type="price",
        name="baseRate",
        title="Base rate",
        alias="",
        quantity=None,
        value=Decimal("500.25"),
        total=Decimal("500.25"),
        is_included_in_total=True,
    )
    return PriceQuote(
        listing_id=9001,
        check_in=check_in,
        check_out=check_out,
        nights=(check_out - check_in).days,
        guests=guests,
        currency=currency,
        total_price=Decimal("500.25"),
        components=(component,),
        calculated_at=timezone.now(),
        envelope_fields=frozenset({"status", "result"}),
        result_field_types=(("totalPrice", "string"),),
        component_field_types=(("value", "string"),),
    )


class FakeClient:
    def __init__(
        self,
        calendar: CalendarDocument,
        *,
        quote_currency: str = "SAR",
        calendar_error: Exception | None = None,
        price_error: Exception | None = None,
    ) -> None:
        self.calendar = calendar
        self.quote_currency = quote_currency
        self.calendar_error = calendar_error
        self.price_error = price_error
        self.calendar_calls = 0
        self.price_calls = 0
        self.price_guests: list[int] = []

    def get_listing_calendar(self, *args: object, **kwargs: object) -> CalendarDocument:
        self.calendar_calls += 1
        if self.calendar_error:
            raise self.calendar_error
        return self.calendar

    def calculate_price(self, *args: object, **kwargs: object) -> PriceQuote:
        self.price_calls += 1
        guests = kwargs["guests"]
        self.price_guests.append(guests)
        if self.price_error:
            raise self.price_error
        return make_quote(
            check_in=kwargs["check_in"],
            check_out=kwargs["check_out"],
            guests=guests,
            currency=self.quote_currency,
        )


def request_for(
    property_obj: Property,
    *,
    check_in: date,
    nights: int = 2,
    guests: int = 2,
) -> AvailabilityRequest:
    return AvailabilityRequest(
        property=property_obj,
        check_in=check_in,
        check_out=check_in + timedelta(days=nights),
        guests=guests,
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("past", PAST_CHECK_IN),
        ("reverse", INVALID_DATES),
        ("same", INVALID_DATES),
        ("capacity", CAPACITY_EXCEEDED),
    ],
)
def test_local_validation_blocks_network(mode: str, expected: str) -> None:
    today = timezone.localdate()
    property_obj = make_property()
    request = request_for(property_obj, check_in=today + timedelta(days=2))
    if mode == "past":
        request = request_for(property_obj, check_in=today - timedelta(days=1))
    elif mode == "reverse":
        request = replace(request, check_out=request.check_in - timedelta(days=1))
    elif mode == "same":
        request = replace(request, check_out=request.check_in)
    elif mode == "capacity":
        request = replace(request, guests=5)
    fake = FakeClient(make_calendar(today + timedelta(days=2)))

    result = AvailabilityService(client=fake).check(request)

    assert result.reason_code == expected
    assert fake.calendar_calls == 0
    assert fake.price_calls == 0


@pytest.mark.parametrize(
    ("visible", "active"),
    [(False, True), (True, False)],
)
def test_hidden_or_inactive_property_blocks_network(visible: bool, active: bool) -> None:
    start = timezone.localdate() + timedelta(days=2)
    property_obj = make_property(visible=visible, active=active)
    fake = FakeClient(make_calendar(start))

    result = AvailabilityService(client=fake).check(request_for(property_obj, check_in=start))

    assert result.reason_code == PROPERTY_UNAVAILABLE
    assert fake.calendar_calls == 0


@pytest.mark.parametrize(
    ("modifier", "expected"),
    [
        (lambda days: days[:-1], CALENDAR_INCOMPLETE),
        (
            lambda days: (replace(days[0], is_available=False), *days[1:]),
            UNAVAILABLE_DATES,
        ),
        (
            lambda days: (replace(days[0], closed_on_arrival=True), *days[1:]),
            CLOSED_ON_ARRIVAL,
        ),
        (
            lambda days: (*days[:-1], replace(days[-1], closed_on_departure=True)),
            CLOSED_ON_DEPARTURE,
        ),
        (
            lambda days: (replace(days[0], minimum_stay=3), *days[1:]),
            MINIMUM_STAY_NOT_MET,
        ),
        (
            lambda days: (replace(days[0], maximum_stay=1), *days[1:]),
            MAXIMUM_STAY_EXCEEDED,
        ),
        (
            lambda days: (
                replace(
                    days[0],
                    is_available=False,
                    available_units_to_sell=0,
                ),
                *days[1:],
            ),
            UNAVAILABLE_DATES,
        ),
        (
            lambda days: (replace(days[0], desired_units_to_sell=0), *days[1:]),
            INVENTORY_CONFLICT,
        ),
    ],
)
def test_calendar_restrictions_are_conservative(
    modifier: object,
    expected: str,
) -> None:
    start = timezone.localdate() + timedelta(days=3)
    base_days = make_calendar(start).days
    modified_days = modifier(base_days)
    reason = evaluate_calendar(
        tuple(modified_days),
        check_in=start,
        check_out=start + timedelta(days=2),
    )
    assert reason == expected


def test_available_calendar_calls_price_and_uses_hostaway_total() -> None:
    cache.clear()
    start = timezone.localdate() + timedelta(days=3)
    property_obj = make_property()
    fake = FakeClient(make_calendar(start))

    result = AvailabilityService(client=fake).check(request_for(property_obj, check_in=start))

    assert result.reason_code == AVAILABLE
    assert result.is_available is True
    assert result.quote is not None
    assert result.quote.total_price == Decimal("500.25")
    assert fake.calendar_calls == 1
    assert fake.price_calls == 1


def test_price_is_not_called_when_calendar_is_unavailable() -> None:
    cache.clear()
    start = timezone.localdate() + timedelta(days=3)
    property_obj = make_property()
    calendar = make_calendar(start)
    calendar = replace(
        calendar,
        days=(replace(calendar.days[0], is_available=False), *calendar.days[1:]),
    )
    fake = FakeClient(calendar)

    result = AvailabilityService(client=fake).check(request_for(property_obj, check_in=start))

    assert result.reason_code == UNAVAILABLE_DATES
    assert fake.price_calls == 0


def test_calendar_timeout_has_public_safe_reason() -> None:
    cache.clear()
    start = timezone.localdate() + timedelta(days=3)
    property_obj = make_property()
    fake = FakeClient(
        make_calendar(start),
        calendar_error=HostawayTimeoutError("synthetic internal timeout"),
    )
    result = AvailabilityService(client=fake).check(request_for(property_obj, check_in=start))
    assert result.reason_code == HOSTAWAY_TEMPORARILY_UNAVAILABLE
    assert "synthetic" not in result.user_message_ar


def test_price_timeout_has_public_safe_reason() -> None:
    cache.clear()
    start = timezone.localdate() + timedelta(days=3)
    property_obj = make_property()
    fake = FakeClient(
        make_calendar(start),
        price_error=HostawayTimeoutError("synthetic internal timeout"),
    )
    result = AvailabilityService(client=fake).check(request_for(property_obj, check_in=start))
    assert result.reason_code == HOSTAWAY_TEMPORARILY_UNAVAILABLE


def test_cache_hit_avoids_repeating_calendar_and_price() -> None:
    cache.clear()
    start = timezone.localdate() + timedelta(days=3)
    property_obj = make_property()
    fake = FakeClient(make_calendar(start))
    service = AvailabilityService(client=fake)
    request = request_for(property_obj, check_in=start)

    first = service.check(request)
    second = service.check(request)

    assert first.calendar_cache_hit is False
    assert first.price_cache_hit is False
    assert second.calendar_cache_hit is True
    assert second.price_cache_hit is True
    assert fake.calendar_calls == 1
    assert fake.price_calls == 1


def test_guest_count_changes_price_cache_key() -> None:
    cache.clear()
    start = timezone.localdate() + timedelta(days=3)
    property_obj = make_property()
    fake = FakeClient(make_calendar(start))
    service = AvailabilityService(client=fake)

    service.check(request_for(property_obj, check_in=start, guests=1))
    service.check(request_for(property_obj, check_in=start, guests=2))

    assert fake.calendar_calls == 1
    assert fake.price_calls == 2
    assert fake.price_guests == [1, 2]


def test_currency_mismatch_uses_quote_currency_and_logs_no_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cache.clear()
    start = timezone.localdate() + timedelta(days=3)
    property_obj = make_property()
    fake = FakeClient(make_calendar(start), quote_currency="USD")
    result = AvailabilityService(client=fake).check(request_for(property_obj, check_in=start))
    assert result.quote is not None
    assert result.quote.currency == "USD"
    assert "differs" in caplog.text


def test_check_does_not_change_business_tables() -> None:
    cache.clear()
    start = timezone.localdate() + timedelta(days=3)
    property_obj = make_property()
    before = (
        Property.objects.count(),
        PropertyImage.objects.count(),
        Review.objects.count(),
    )
    AvailabilityService(client=FakeClient(make_calendar(start))).check(
        request_for(property_obj, check_in=start)
    )
    after = (
        Property.objects.count(),
        PropertyImage.objects.count(),
        Review.objects.count(),
    )
    assert after == before


def test_inventory_conflict_prevents_price_details() -> None:
    cache.clear()
    start = timezone.localdate() + timedelta(days=3)
    property_obj = make_property()
    calendar = make_calendar(start)
    calendar = replace(
        calendar,
        days=(
            replace(
                calendar.days[0],
                is_available=False,
                available_units_to_sell=1,
            ),
            *calendar.days[1:],
        ),
    )
    fake = FakeClient(calendar)

    result = AvailabilityService(client=fake).check(request_for(property_obj, check_in=start))

    assert result.reason_code == INVENTORY_CONFLICT
    assert fake.price_calls == 0


def test_no_available_window_in_365_days_does_not_price() -> None:
    cache.clear()
    start = timezone.localdate()
    property_obj = make_property()
    calendar = CalendarDocument(
        days=tuple(
            make_day(start + timedelta(days=offset), is_available=False) for offset in range(366)
        ),
        envelope_fields=frozenset({"status", "result"}),
        day_field_types=(("date", "string"),),
    )
    fake = FakeClient(calendar)

    request, result, _calendar = AvailabilityService(client=fake).find_first_available(
        property_obj=property_obj,
        start_date=start,
        scan_days=365,
        stay_nights=2,
        guests=2,
        bypass_cache=True,
    )

    assert request is None
    assert result.reason_code == UNAVAILABLE_DATES
    assert fake.calendar_calls == 1
    assert fake.price_calls == 0


def test_first_available_window_prices_exactly_once() -> None:
    cache.clear()
    start = timezone.localdate()
    property_obj = make_property()
    fake = FakeClient(make_calendar(start, nights=10))

    request, result, _calendar = AvailabilityService(client=fake).find_first_available(
        property_obj=property_obj,
        start_date=start,
        scan_days=10,
        stay_nights=2,
        guests=2,
        bypass_cache=True,
    )

    assert request is not None
    assert request.check_in == start
    assert result.reason_code == AVAILABLE
    assert fake.calendar_calls == 1
    assert fake.price_calls == 1
