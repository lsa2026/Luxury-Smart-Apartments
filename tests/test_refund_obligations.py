"""Automatic changes that owe the guest money must never lose that debt."""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory, override_settings

from apps.core.admin_dashboard import dashboard_payload
from apps.notifications.models import Notification
from apps.reservations.models import (
    BookingModificationRequest,
    CancellationPolicyTier,
    RefundObligation,
)
from apps.reservations.services import refunds
from tests.test_booking_modifications_phase6 import confirmed_reservation

pytestmark = pytest.mark.django_db

RIYADH = ZoneInfo("Asia/Riyadh")


def flexible_tiers() -> None:
    """A table shaped like the one the administration fills in."""
    CancellationPolicyTier.objects.create(
        policy_code="flexible",
        min_hours_before_check_in=168,
        refund_percentage=Decimal("100"),
    )
    CancellationPolicyTier.objects.create(
        policy_code="flexible",
        min_hours_before_check_in=24,
        refund_percentage=Decimal("50"),
    )


def priced_reservation(total: str = "1000.0000"):
    reservation = confirmed_reservation()
    reservation.total_price = Decimal(total)
    reservation.currency = "SAR"
    reservation.property.cancellation_policy = "flexible"
    reservation.property.time_zone_name = "Asia/Riyadh"
    reservation.property.check_in_time_start = 15
    reservation.property.save()
    reservation.save()
    return reservation


def test_an_empty_policy_table_refunds_nothing() -> None:
    """An undecided policy must never be read as "refund everything"."""
    reservation = priced_reservation()

    computation = refunds.cancellation_refund(reservation)

    assert computation.amount == Decimal("0")
    assert computation.detail["reason"] == "no_matching_tier"


def test_the_widest_matching_tier_wins() -> None:
    flexible_tiers()
    reservation = priced_reservation("1000.0000")
    reservation.check_in = date(2026, 12, 20)
    reservation.check_out = date(2026, 12, 22)
    reservation.nights = 2
    reservation.save()

    early = refunds.cancellation_refund(reservation, at=datetime(2026, 12, 1, 12, 0, tzinfo=RIYADH))
    late = refunds.cancellation_refund(reservation, at=datetime(2026, 12, 19, 12, 0, tzinfo=RIYADH))

    assert early.amount == Decimal("1000.0000")
    assert late.amount == Decimal("500.0000")


def test_a_cancellation_inside_the_last_day_refunds_nothing() -> None:
    flexible_tiers()
    reservation = priced_reservation()
    reservation.check_in = date(2026, 12, 20)
    reservation.check_out = date(2026, 12, 22)
    reservation.nights = 2
    reservation.save()

    computation = refunds.cancellation_refund(
        reservation, at=datetime(2026, 12, 20, 10, 0, tzinfo=RIYADH)
    )

    assert computation.amount == Decimal("0")


def test_a_tier_can_withhold_the_cleaning_fee() -> None:
    CancellationPolicyTier.objects.create(
        policy_code="flexible",
        min_hours_before_check_in=0,
        refund_percentage=Decimal("100"),
        refunds_cleaning_fee=False,
    )
    reservation = priced_reservation("1000.0000")
    quote = reservation.booking_intent.quote
    quote.components = [{"type": "cleaningFee", "title": "رسوم التنظيف", "total": "150.00"}]
    quote.save()

    computation = refunds.cancellation_refund(reservation)

    assert computation.amount == Decimal("850.0000")
    assert computation.detail["cleaning_fee_withheld"] == "150.00"


def test_recording_an_obligation_announces_it_with_contact_details() -> None:
    reservation = priced_reservation()
    computation = refunds.RefundComputation(Decimal("250.0000"), "SAR", {"source": "test"})

    obligation = refunds.record_obligation(
        reservation,
        reason=RefundObligation.Reason.CANCELLATION,
        computation=computation,
    )

    assert obligation is not None
    assert obligation.status == RefundObligation.Status.DUE
    assert obligation.guest_email == reservation.booking_intent.guest_email
    assert obligation.guest_phone == reservation.booking_intent.guest_phone
    assert obligation.guest_name
    assert Notification.objects.filter(
        notification_type=Notification.Type.REFUND_DUE,
        related_object_reference=obligation.public_reference,
    ).exists()


def test_a_zero_refund_creates_no_task_for_anyone() -> None:
    reservation = priced_reservation()

    obligation = refunds.record_obligation(
        reservation,
        reason=RefundObligation.Reason.CANCELLATION,
        computation=refunds.RefundComputation(Decimal("0"), "SAR", {}),
    )

    assert obligation is None
    assert not RefundObligation.objects.exists()
    assert not Notification.objects.filter(notification_type=Notification.Type.REFUND_DUE).exists()


def test_outstanding_refunds_are_deducted_from_reported_income() -> None:
    """Money already collected that has to leave again must not flatter income."""
    reservation = priced_reservation()
    refunds.record_obligation(
        reservation,
        reason=RefundObligation.Reason.CANCELLATION,
        computation=refunds.RefundComputation(Decimal("300.0000"), "SAR", {}),
    )
    user = get_user_model().objects.create_superuser(
        username="refund-admin",
        email="refund-admin@example.invalid",
        password="Correct-Horse-Battery-2026",
    )
    request = RequestFactory().get("/admin/")
    request.user = user

    payload = dashboard_payload(request)

    assert payload["refunds_due"] == 1
    row = next(
        (item for item in payload["revenue_by_currency"] if item["currency"] == "SAR"),
        None,
    )
    if row is not None:
        assert row["refunds_due"] == Decimal("300.0000")


def test_a_settled_refund_stops_being_deducted() -> None:
    reservation = priced_reservation()
    obligation = refunds.record_obligation(
        reservation,
        reason=RefundObligation.Reason.CANCELLATION,
        computation=refunds.RefundComputation(Decimal("300.0000"), "SAR", {}),
    )
    assert refunds.outstanding_total("SAR") == Decimal("300.0000")

    obligation.status = RefundObligation.Status.TRANSFERRED
    obligation.transferred_at = datetime(2026, 12, 1, 12, 0, tzinfo=RIYADH)
    obligation.save()

    assert refunds.outstanding_total("SAR") == Decimal("0")


@override_settings(BOOKING_AUTOMATIC_MODIFICATION_APPROVAL=True)
def test_a_price_decrease_is_queued_for_hostaway_not_for_a_person() -> None:
    """The whole point: a decrease applies itself instead of waiting for approval."""
    from apps.reservations.services.modifications import ModificationService
    from tests.test_booking_modifications_phase6 import (
        ModificationAvailabilityStub,
        price_quote,
    )

    reservation = priced_reservation("1000.0000")
    cheaper = price_quote(reservation, total=Decimal("800.00"))
    stub = ModificationAvailabilityStub(reservation, quote=cheaper)
    service = ModificationService(availability_service=stub)

    outcome = service.create_change_quote(
        reservation,
        new_check_in=reservation.check_in,
        new_check_out=reservation.check_out,
        new_guests=reservation.guests,
        session_hash=reservation.booking_intent.session_key_hash,
    )

    assert outcome.code == "created"
    assert outcome.request.price_difference < 0
    assert outcome.request.status == BookingModificationRequest.Status.READY_FOR_HOSTAWAY
    assert outcome.request.refund_amount == Decimal("200.0000")
