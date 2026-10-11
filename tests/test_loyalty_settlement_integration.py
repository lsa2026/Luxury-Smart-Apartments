"""Cancellation, modification and manual receipts: factual local workflows only."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings
from django.utils import timezone

from apps.loyalty.models import Ledger
from apps.loyalty.services import balance, hold, reconcile, redeem
from apps.loyalty.settlements import (
    cancellation_plan,
    decrease_plan,
    funding,
    payment_balances,
    record_contract_nights,
    record_manual_receipt,
    record_refund_plan,
)
from apps.loyalty.stay import record_departure
from apps.payments.hyperpay.refunds import HyperPayRefundService
from apps.payments.models import PaymentAttempt
from apps.reservations.models import (
    BookingModificationRequest,
    ManualBookingDraft,
    RefundObligation,
    Reservation,
)
from apps.reservations.services.automatic_modifications import execute_automatic_modification
from tests.test_hyperpay_refunds_phase94 import REFUND_SETTINGS, RefundStub
from tests.test_loyalty_stage8 import booking, join, payment, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def program(settings, monkeypatch):
    settings.LOYALTY_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = False
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    return clock


def owner():
    u = user("settlement-owner")
    u.is_staff = u.is_superuser = True
    u.save()
    return u


def mixed(*, arrived=False):
    u = user("mixed-settlement")
    m = join(u)
    reconcile(booking(u, "10000.00").pk)
    r = booking(u, "750.00", paid=False, arrived=False)
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save()
    hold(u, "TEST-MIXED-SETTLE-HOLD", r.pk, "100.00")
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CONFIRMED
    if arrived:
        r.loyalty_state["arrival"] = {
            "at": (timezone.now() - timedelta(minutes=30)).isoformat(),
            "reference": "TEST-ARRIVAL",
            "operator": 0,
        }
    r.save()
    redeem(u, "TEST-MIXED-SETTLE-REDEEM", r.pk)
    payment(r, "650.00")
    r.refresh_from_db()
    return u, m, r


def change(r, *, total=None, complete=True):
    return BookingModificationRequest.objects.create(
        reservation=r,
        request_type="cancel_reservation" if total is None else "change_dates",
        status="completed" if complete else "ready_for_hostaway",
        old_check_in=r.check_in,
        old_check_out=r.check_out,
        old_guests=r.guests,
        old_total=r.total_price,
        new_total=total,
        new_check_in=r.check_in if total is not None else None,
        new_check_out=r.check_out if total is not None else None,
        new_guests=r.guests if total is not None else None,
        price_difference=Decimal(total) - r.total_price if total is not None else 0,
        currency="SAR",
        idempotency_key=f"TEST-CHANGE-{BookingModificationRequest.objects.count()}",
        session_key_hash=r.booking_intent.session_key_hash,
        expires_at=timezone.now() + timedelta(hours=1),
    )


def test_prearrival_refund_returns_exact_points_and_cash_once():
    _, m, r = mixed()
    plan = cancellation_plan(r)
    assert plan["cash"] == 650 and plan["points"] == 100
    mod = change(r)
    r.normalized_status = Reservation.Status.CANCELLED
    r.save()
    rows = record_refund_plan(mod, plan, RefundObligation.Reason.CANCELLATION)
    assert len(rows) == 1 and rows[0].amount == 650
    assert balance(m) == 300
    assert record_refund_plan(mod, plan, RefundObligation.Reason.CANCELLATION)[0].pk == rows[0].pk
    assert Ledger.objects.filter(reservation=r, event_type="restore").count() == 1


def test_decrease_refunds_original_funding_proportion_without_new_card_charge():
    _, m, r = mixed()
    plan = decrease_plan(r, Decimal("600.00"))
    assert plan["cash"] == 130 and plan["points"] == 20
    mod = change(r, total="600.00")
    r.total_price = 600
    r.save()
    rows = record_refund_plan(mod, plan, RefundObligation.Reason.MODIFICATION_DECREASE)
    assert rows[0].amount == 130 and balance(m) == 220
    assert PaymentAttempt.objects.filter(booking_intent=r.booking_intent).count() == 1
    assert funding(Reservation.objects.get(pk=r.pk)) == (Decimal("520.00"), Decimal("80.00"))


def test_more_than_one_actual_receipt_is_allocated_and_refunded_to_its_own_provider_id():
    u = user("multiple-receipts")
    join(u)
    r = booking(u, "1000.00", paid=False, arrived=False)
    p1 = payment(r, "750.00")
    mod_paid = change(r, total="1000.00")
    p2 = payment(r, "250.00")
    p2.modification_request = mod_paid
    p2.save()
    mod = change(r)
    plan = cancellation_plan(r)
    r.normalized_status = Reservation.Status.CANCELLED
    r.save()
    rows = record_refund_plan(mod, plan, RefundObligation.Reason.CANCELLATION)
    assert {row.calculation["payment_attempt"]: row.amount for row in rows} == {
        str(p1.pk): Decimal("750.00"),
        str(p2.pk): Decimal("250.00"),
    }
    stub = RefundStub({"id": "TEST-REFUND", "result": {"code": "000.100.110"}})
    with override_settings(**REFUND_SETTINGS):
        for row in rows:
            HyperPayRefundService(client=stub).submit(row, operator=object())
    assert {call[0] for call in stub.calls} == {p1.provider_payment_id, p2.provider_payment_id}
    p1.refresh_from_db()
    p2.refresh_from_db()
    assert p1.status == p2.status == PaymentAttempt.Status.REFUNDED
    assert all(value == 0 for _, value in payment_balances(r))


def test_automatic_cancellation_submits_all_allocated_receipts_after_confirmation():
    u = user("automatic-multi")
    join(u)
    r = booking(u, "1000.00", paid=False, arrived=False)
    payment(r, "700.00")
    payment(r, "300.00")
    mod = change(r, complete=False)
    calls = []

    def inventory(request):
        calls.append("inventory")
        request.status = request.Status.COMPLETED
        request.save()
        request.reservation.normalized_status = Reservation.Status.CANCELLED
        request.reservation.save()
        return SimpleNamespace(code="completed", request=request)

    stub = RefundStub({"id": "TEST-REFUND", "result": {"code": "000.100.110"}})
    with override_settings(
        **{
            **REFUND_SETTINGS,
            "BOOKING_AUTOMATIC_CANCELLATION_ENABLED": True,
            "BOOKING_AUTOMATIC_REFUND_ENABLED": True,
        }
    ):
        outcome = execute_automatic_modification(
            mod,
            service=SimpleNamespace(execute=inventory),
            refund_service=HyperPayRefundService(client=stub),
        )
    assert outcome.code == "completed" and calls == ["inventory"]
    assert len(stub.calls) == 2
    assert r.refund_obligations.filter(status="transferred").count() == 2


def test_started_stay_without_actual_departure_blocks_cancellation_before_provider():
    _, _, r = mixed(arrived=True)
    with pytest.raises(ValidationError):
        cancellation_plan(r)
    mod = change(r, complete=False)
    calls = []
    with override_settings(BOOKING_AUTOMATIC_CANCELLATION_ENABLED=True):
        outcome = execute_automatic_modification(
            mod, service=SimpleNamespace(execute=lambda _: calls.append("inventory"))
        )
    assert outcome.code == "loyalty_departure_evidence_required" and not calls


@pytest.mark.parametrize("hour,refundable", [(19, "750.00"), (20, "350.00")])
def test_actual_departure_uses_saved_different_nightly_prices(program, hour, refundable):
    _, _, r = mixed(arrived=True)
    reconcile(r.pk)
    operator = owner()
    record_contract_nights(
        operator,
        r.pk,
        nights=[
            {"date": r.check_in.isoformat(), "value": "400.00"},
            {"date": (r.check_in + timedelta(days=1)).isoformat(), "value": "350.00"},
        ],
        reference="TEST-CONTRACT",
    )
    program[0] = program[0].replace(hour=hour - 3)
    result = record_departure(operator, r.pk, vacated_at=program[0], reference="TEST-DEPARTURE")
    assert Decimal(result["refundable_total"]) == Decimal(refundable)
    r.refresh_from_db()
    plan = cancellation_plan(r)
    assert plan["cash"] + plan["points"] == Decimal(refundable)


def test_contract_nights_cannot_be_approximated_or_written_by_guest():
    u, _, r = mixed(arrived=True)
    with pytest.raises(PermissionDenied):
        record_contract_nights(u, r.pk, nights=[], reference="TEST")
    with pytest.raises(ValidationError):
        record_contract_nights(
            owner(),
            r.pk,
            nights=[{"date": r.check_in.isoformat(), "value": "750.00"}],
            reference="TEST",
        )


def manual_booking(u):
    r = booking(u, "750.00", paid=False, arrived=True)
    ManualBookingDraft.objects.create(
        quote=r.booking_intent.quote,
        property=r.property,
        check_in=r.check_in,
        check_out=r.check_out,
        nights=r.nights,
        guests=r.guests,
        currency="SAR",
        system_total_price=750,
        final_total_price=750,
        availability_checked_at=timezone.now(),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return r


def test_trusted_manual_receipt_awards_on_arrival_and_never_calls_gateway():
    u = user("manual-receipt")
    m = join(u)
    r = manual_booking(u)
    operator = owner()
    row = record_manual_receipt(
        operator,
        r.pk,
        value="750.00",
        received_at=timezone.now(),
        reference="TEST-BANK-RECEIPT",
        lodging_only=True,
    )
    assert row.provider == "manual" and row.amount == 750
    assert (
        record_manual_receipt(
            operator,
            r.pk,
            value="750.00",
            received_at=timezone.now(),
            reference="TEST-BANK-RECEIPT",
            lodging_only=True,
        ).pk
        == row.pk
    )
    reconcile(r.pk)
    assert balance(m) == Decimal("22.50")
    assert not PaymentAttempt.objects.filter(
        booking_intent=r.booking_intent, provider="hyperpay"
    ).exists()


def test_manual_receipt_requires_owner_and_explicit_lodging_attestation():
    u = user("manual-security")
    join(u)
    r = manual_booking(u)
    with pytest.raises(PermissionDenied):
        record_manual_receipt(
            u, r.pk, value="750.00", received_at=timezone.now(), reference="TEST", lodging_only=True
        )
    with pytest.raises(ValidationError):
        record_manual_receipt(
            owner(), r.pk, value="750.00", received_at=timezone.now(), reference="TEST"
        )
    assert not PaymentAttempt.objects.filter(booking_intent=r.booking_intent).exists()


def test_manual_receipt_rejects_excess_and_duplicate_reference_changes():
    u = user("manual-ceiling")
    join(u)
    r = manual_booking(u)
    operator = owner()
    with pytest.raises(ValidationError):
        record_manual_receipt(
            operator,
            r.pk,
            value="750.01",
            received_at=timezone.now(),
            reference="TEST",
            lodging_only=True,
        )
    record_manual_receipt(
        operator,
        r.pk,
        value="300.00",
        received_at=timezone.now(),
        reference="TEST",
        lodging_only=True,
    )
    with pytest.raises(ValidationError):
        record_manual_receipt(
            operator,
            r.pk,
            value="400.00",
            received_at=timezone.now(),
            reference="TEST",
            lodging_only=True,
        )
