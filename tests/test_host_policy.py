"""The host's listing policy, and what it refuses for a guest's change request."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.integrations.hostaway.listing_validators import normalize_listing
from apps.reservations.services import host_policy
from tests.test_booking_models_services import make_property

pytestmark = pytest.mark.django_db

RIYADH = ZoneInfo("Asia/Riyadh")


def policy_property(**fields: object):
    property_obj = make_property()
    for name, value in fields.items():
        setattr(property_obj, name, value)
    property_obj.save()
    return property_obj


def test_a_listing_payload_carries_the_policy_into_the_project() -> None:
    """These are the exact values this account reports for its listings."""
    listing = normalize_listing(
        {
            "id": 315816,
            "minNights": 1,
            "maxNights": 365,
            "cancellationPolicy": "flexible",
            "allowSameDayBooking": 0,
            "sameDayBookingLeadTime": 12,
            "checkInTimeStart": 15,
            "checkOutTime": 12,
            "instantBookable": 1,
            "timeZoneName": "Asia/Riyadh",
        }
    )

    assert listing.min_nights == 1
    assert listing.max_nights == 365
    assert listing.cancellation_policy == "flexible"
    assert listing.allow_same_day_booking is False
    assert listing.same_day_booking_lead_time_hours == 12
    assert listing.check_in_time_start == 15
    assert listing.check_out_time == 12
    assert listing.instant_bookable is True
    assert listing.time_zone_name == "Asia/Riyadh"
    assert listing.validation_errors == ()


def test_a_listing_without_policy_fields_reports_none_not_zero() -> None:
    """None has to stay distinguishable from "no restriction"."""
    listing = normalize_listing({"id": 315816})

    assert listing.min_nights is None
    assert listing.max_nights is None
    assert listing.cancellation_policy == ""
    assert listing.allow_same_day_booking is None
    assert listing.same_day_booking_lead_time_hours is None
    assert listing.validation_errors == ()


def test_a_nonsense_flag_is_isolated_rather_than_failing_the_listing() -> None:
    listing = normalize_listing({"id": 315816, "allowSameDayBooking": 7})

    assert listing.allow_same_day_booking is None
    assert any("allowSameDayBooking" in error for error in listing.validation_errors)


def test_stays_shorter_than_the_minimum_are_refused() -> None:
    property_obj = policy_property(min_nights=3)

    refusal = host_policy.policy_blocker(
        property_obj,
        check_in=date(2026, 12, 1),
        check_out=date(2026, 12, 3),
    )

    assert refusal == host_policy.MINIMUM_STAY_NOT_MET


def test_stays_longer_than_the_maximum_are_refused() -> None:
    property_obj = policy_property(max_nights=5)

    refusal = host_policy.policy_blocker(
        property_obj,
        check_in=date(2026, 12, 1),
        check_out=date(2026, 12, 10),
    )

    assert refusal == host_policy.MAXIMUM_STAY_EXCEEDED


def test_an_unreported_policy_cannot_block() -> None:
    property_obj = policy_property(min_nights=None, max_nights=None)

    refusal = host_policy.policy_blocker(
        property_obj,
        check_in=date(2026, 12, 1),
        check_out=date(2026, 12, 2),
    )

    assert refusal == host_policy.ALLOWED


def test_an_arrival_today_is_refused_when_the_host_forbids_same_day() -> None:
    property_obj = policy_property(
        allow_same_day_booking=False,
        time_zone_name="Asia/Riyadh",
        check_in_time_start=15,
    )
    now = datetime(2026, 12, 1, 8, 0, tzinfo=RIYADH)

    refusal = host_policy.policy_blocker(
        property_obj,
        check_in=date(2026, 12, 1),
        check_out=date(2026, 12, 3),
        now=now,
    )

    assert refusal == host_policy.SAME_DAY_CHANGE_NOT_ALLOWED


def test_a_future_arrival_is_untouched_by_the_same_day_rule() -> None:
    property_obj = policy_property(
        allow_same_day_booking=False,
        time_zone_name="Asia/Riyadh",
    )
    now = datetime(2026, 12, 1, 8, 0, tzinfo=RIYADH)

    refusal = host_policy.policy_blocker(
        property_obj,
        check_in=date(2026, 12, 2),
        check_out=date(2026, 12, 4),
        now=now,
    )

    assert refusal == host_policy.ALLOWED


def test_the_lead_time_is_measured_to_the_real_arrival_hour() -> None:
    property_obj = policy_property(
        allow_same_day_booking=True,
        same_day_booking_lead_time_hours=12,
        check_in_time_start=15,
        time_zone_name="Asia/Riyadh",
    )
    arrival_day = date(2026, 12, 1)

    early = host_policy.policy_blocker(
        property_obj,
        check_in=arrival_day,
        check_out=date(2026, 12, 3),
        now=datetime(2026, 12, 1, 2, 0, tzinfo=RIYADH),
    )
    late = host_policy.policy_blocker(
        property_obj,
        check_in=arrival_day,
        check_out=date(2026, 12, 3),
        now=datetime(2026, 12, 1, 9, 0, tzinfo=RIYADH),
    )

    assert early == host_policy.ALLOWED
    assert late == host_policy.ARRIVAL_LEAD_TIME_NOT_MET


def test_check_in_moment_uses_the_listing_timezone_and_hour() -> None:
    property_obj = policy_property(check_in_time_start=15, time_zone_name="Asia/Riyadh")

    moment = host_policy.check_in_datetime(property_obj, date(2026, 12, 1))

    assert moment == datetime(2026, 12, 1, 15, 0, tzinfo=RIYADH)


def test_an_unknown_timezone_falls_back_instead_of_raising() -> None:
    property_obj = policy_property(time_zone_name="Not/AZone", check_in_time_start=15)

    moment = host_policy.check_in_datetime(property_obj, date(2026, 12, 1))

    assert moment.hour == 15
    assert host_policy.listing_timezone(property_obj) is None


def test_midnight_check_in_belongs_to_the_following_day() -> None:
    property_obj = policy_property(check_in_time_start=24, time_zone_name="Asia/Riyadh")

    moment = host_policy.check_in_datetime(property_obj, date(2026, 12, 1))

    assert moment == datetime(2026, 12, 2, 0, 0, tzinfo=RIYADH)


def test_a_property_without_a_listing_never_blocks() -> None:
    assert (
        host_policy.policy_blocker(
            None,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=2),
        )
        == host_policy.ALLOWED
    )
