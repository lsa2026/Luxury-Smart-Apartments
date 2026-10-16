"""Real site models, fictional guests, strict offline runner; no production mutations."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import profile_for
from apps.loyalty.calculation import amount, cents, departure_refund, refund_split, reward_plan
from apps.loyalty.checkout import cash_due, confirm_points_booking
from apps.loyalty.models import Ledger, Referral
from apps.loyalty.services import (
    balance,
    confirm_arrival,
    enroll,
    hold,
    issue_invite,
    reconcile,
    redeem,
    release,
    restore_points,
    statement,
    transfer,
    withdraw,
)
from apps.loyalty.site_adapter import schedule_reconciliation
from apps.payments.hyperpay.service import HyperPayService
from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingModificationRequest, Reservation
from tests.test_account_booking_claim import make_reservation
from tests.test_hyperpay import HYPERPAY_SETTINGS, HyperPayStub, payable_intent

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def local_program(settings, monkeypatch):
    settings.LOYALTY_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    return clock


def user(number, *, verified=True):
    u = get_user_model().objects.create_user(
        username=f"loyalty-{number}",
        email=f"loyalty-{number}@example.invalid",
        first_name=f"ضيف {number}",
    )
    if verified:
        profile_for(u).mark_verified()
    return u


def join(u, **kwargs):
    return enroll(
        u,
        consent=True,
        terms_version="test-not-published-v1",
        terms_reference="isolated-consent-reference",
        **kwargs,
    )


def booking(u, value="750.00", fees="0.00", *, paid=True, arrived=True, reference=None):
    total = Decimal(value) + Decimal(fees)
    r = make_reservation(reference or f"TEST-LOYALTY-{u.pk}-{Reservation.objects.count()}")
    start = timezone.now().date()
    end = start + timedelta(days=2)
    r.booking_intent.customer = u
    r.booking_intent.total_price = total
    r.booking_intent.check_in = start
    r.booking_intent.check_out = end
    r.booking_intent.save()
    q = r.booking_intent.quote
    q.total_price = total
    q.check_in = start
    q.check_out = end
    q.components = [{"type": "baseRate", "total": value, "included_in_total": True}]
    if Decimal(fees):
        q.components.append({"type": "cleaningFee", "total": fees, "included_in_total": True})
    q.save()
    r.property.time_zone_name = "Asia/Riyadh"
    r.property.save(update_fields=["time_zone_name"])
    r.total_price, r.check_in, r.check_out = total, start, end
    r.source_type = Reservation.SourceType.DIRECT_WEBSITE
    r.normalized_status, r.is_test = Reservation.Status.CONFIRMED, True
    if arrived:
        r.loyalty_state = {
            "arrival": {
                "at": datetime.combine(start, datetime.min.time(), UTC)
                .replace(hour=12, minute=5)
                .isoformat(),
                "reference": "TEST-RECEPTION-ARRIVAL",
                "operator": 0,
            }
        }
    r.save()
    if paid:
        payment(r, total)
    return r


def payment(r, value, *, verified=True, status=PaymentAttempt.Status.SUCCEEDED):
    n = PaymentAttempt.objects.count()
    return PaymentAttempt.objects.create(
        booking_intent=r.booking_intent,
        amount=value,
        currency="SAR",
        provider="hyperpay",
        status=status,
        idempotency_key=f"TEST-PAYMENT-{n}",
        provider_payment_id=f"TEST-PROVIDER-{n}",
        verified_at=timezone.now() if verified else None,
    )


def completed_inviter(u, clock):
    member = join(u)
    r = booking(u)
    reconcile(r.pk)
    clock[0] += timedelta(days=3)
    token = issue_invite(u, r.pk)
    return member, token


def test_native_four_guest_sequence_matches_the_master_example(local_program):
    a, b, c, d = [user(n) for n in range(1, 5)]
    am, token = completed_inviter(a, local_program)
    bm = join(b, invitation_token=token, confirmed_inviter=am.pk)
    br = booking(b, "950.00")
    reconcile(br.pk)
    local_program[0] += timedelta(days=3)
    bt = issue_invite(b, br.pk)
    cm = join(c, invitation_token=bt, confirmed_inviter=bm.pk)
    dm = join(d, invitation_token=bt, confirmed_inviter=bm.pk)
    reconcile(booking(c, "850.00").pk)
    reconcile(booking(d, "650.00").pk)
    transfer(a, "TEST-TRANSFER-20", dm.number, "20.00")
    assert [balance(m) for m in [am, bm, cm, dm]] == list(
        map(
            Decimal,
            [
                "65.00",
                "103.50",
                "25.50",
                "39.50",
            ],
        )
    )
    rows = list(Ledger.objects.all())
    assert len(rows) == 11
    running, personal = Decimal(0), {}
    for row in rows:
        running += row.credit - row.debit
        personal[row.member_id] = personal.get(row.member_id, Decimal(0)) + row.credit - row.debit
        assert row.program_balance_after == running
        assert row.member_balance_after == personal[row.member_id]
        assert row.membership_number == row.member.number
        assert row.tree_number == row.member.referral.tree_number
        if row.event_type == "earn":
            assert row.arrival_reference and row.evidence["payments"]
            assert row.booking_customer_id == row.reservation.booking_intent.customer_id
    assert running == Decimal("233.50")
    assert cm.referral.tree_number == "0001-0001-0001"
    assert dm.referral.tree_number == "0001-0001-0002"


@pytest.mark.parametrize(
    "missing,expected",
    [
        ("arrival", "arrival_required"),
        ("membership", "membership_required"),
        ("verified", "verified_payment_incomplete"),
        ("cash", "verified_payment_incomplete"),
    ],
)
def test_no_award_without_each_fact(missing, expected):
    u = user(1)
    if missing != "membership":
        join(u)
    r = booking(u, paid=missing not in {"verified", "cash"}, arrived=missing != "arrival")
    if missing == "verified":
        payment(r, r.total_price, verified=False)
    assert reconcile(r.pk)["status"] == expected
    assert not Ledger.objects.exists()


def test_join_during_paid_stay_adds_once_without_waiting_for_checkout():
    u = user(1)
    r = booking(u)
    assert reconcile(r.pk)["status"] == "membership_required"
    member = join(u)
    assert balance(member) == Decimal("22.50")
    assert reconcile(r.pk)["movements"] == 0
    assert join(u).pk == member.pk
    assert Ledger.objects.count() == 1


def test_join_after_checkout_is_not_retroactive(local_program):
    u = user(1)
    r = booking(u)
    local_program[0] += timedelta(days=3)
    member = join(u)
    assert reconcile(r.pk)["status"] == "stay_or_consent_not_eligible"
    assert balance(member) == 0


@pytest.mark.parametrize("source", ["unknown", "hostaway_manual", "external_channel"])
def test_external_channels_never_awarded(source):
    u = user(1)
    join(u)
    r = booking(u)
    r.source_type = source
    r.save()
    assert reconcile(r.pk)["status"] == "outside_program_scope"
    assert not Ledger.objects.exists()


def test_paid_status_text_is_not_proof_of_receipt():
    u = user(1)
    join(u)
    r = booking(u, paid=False)
    r.payment_status = "paid"
    r.save()
    assert reconcile(r.pk)["movements"] == 0


def test_past_booking_before_launch_is_excluded(settings):
    u = user(1)
    member = join(u)
    r = booking(u)
    settings.LOYALTY_LAUNCH_AT = (r.created_at + timedelta(seconds=1)).isoformat()
    assert reconcile(r.pk)["status"] == "outside_program_scope"
    assert balance(member) == 0


def test_reception_confirmation_enforces_permission_and_server_evidence():
    u = user(1)
    member = join(u)
    r = booking(u, arrived=False)
    with pytest.raises(PermissionDenied):
        confirm_arrival(u, r.pk, arrived_at=timezone.now(), reference="TEST")
    operator = get_user_model().objects.create_superuser(username="TEST-RECEPTION", password=None)
    assert (
        confirm_arrival(operator, r.pk, arrived_at=timezone.now(), reference="TEST-ARRIVAL")[
            "movements"
        ]
        == 1
    )
    assert balance(member) == Decimal("22.50")
    assert (
        confirm_arrival(operator, r.pk, arrived_at=timezone.now(), reference="TEST-ARRIVAL")[
            "movements"
        ]
        == 0
    )
    with pytest.raises(ValidationError):
        confirm_arrival(operator, r.pk, arrived_at=timezone.now(), reference="TEST-ALTERED")


def test_unverified_account_or_missing_consent_cannot_join():
    with pytest.raises(PermissionDenied):
        join(user(1, verified=False))
    with pytest.raises(ValidationError):
        enroll(user(2), consent=False, terms_version="test", terms_reference="test")


def test_invite_only_after_completed_stay_and_explicit_parent_confirmation(local_program):
    u = user(1)
    m = join(u)
    r = booking(u)
    with pytest.raises(ValidationError):
        issue_invite(u, r.pk)
    local_program[0] += timedelta(days=3)
    token = issue_invite(u, r.pk)
    v = user(2)
    with pytest.raises(ValidationError):
        join(v, invitation_token=token, confirmed_inviter="not-the-parent")
    child = join(v, invitation_token=token, confirmed_inviter=m.pk)
    assert child.referral.inviter_id == m.pk
    join(v)  # A later direct visit cannot rewrite attribution.
    child.refresh_from_db()
    assert child.referral.inviter_id == m.pk


def test_fee_exclusion_and_uncertain_breakdown_fail_closed():
    u = user(1)
    m = join(u)
    r = booking(u, "750.00", "100.00")
    assert reconcile(r.pk)["base"] == "750.00"
    assert balance(m) == Decimal("22.50")
    r2 = booking(u)
    q = r2.booking_intent.quote
    q.components = []
    q.save()
    assert reconcile(r2.pk)["status"] == "price_breakdown_missing"
    assert balance(m) == Decimal("22.50")


def test_transfer_two_sides_no_recipient_online_and_duplicate_request():
    u, v = user(1), user(2)
    m, target = join(u), join(v)
    assert not Reservation.objects.filter(booking_intent__customer=v).exists()
    reconcile(booking(u).pk)
    first = transfer(u, "TEST-TX", target.number, "10.00")
    assert transfer(u, "TEST-TX", target.number, "10.00") == first
    assert balance(m) == Decimal("12.50") and balance(target) == Decimal("10.00")
    rows = list(Ledger.objects.filter(operation_group=first["group"]))
    assert len(rows) == 2
    assert rows[0].counterpart_id == rows[1].member_id
    assert rows[1].counterpart_id == rows[0].member_id
    assert rows[0].debit == rows[1].credit
    assert not Reservation.objects.filter(booking_intent__customer=v).exists()
    with pytest.raises(ValidationError):
        transfer(u, "TEST-TX", target.number, "11.00")


def test_transfer_atomic_rollback_on_second_side_failure(monkeypatch):
    import apps.loyalty.services as service

    u, v = user(1), user(2)
    m, target = join(u), join(v)
    reconcile(booking(u).pk)
    post = service._post

    def fail_second(*args, **kwargs):
        if kwargs.get("component") == "transfer-in":
            raise RuntimeError("TEST-ROLLBACK")
        return post(*args, **kwargs)

    monkeypatch.setattr(service, "_post", fail_second)
    with pytest.raises(RuntimeError):
        transfer(u, "TEST-ROLLBACK", target.number, "10.00")
    assert balance(m) == Decimal("22.50") and balance(target) == 0
    assert Ledger.objects.count() == 1


def test_hold_release_redeem_restore_and_no_double_spending():
    u = user(1)
    m = join(u)
    reconcile(booking(u).pk)
    r = booking(u, "20.00", paid=False, arrived=False)
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save()
    hold(u, "TEST-HOLD", r.pk, "20.00")
    assert statement(u)["available"] == Decimal("2.50")
    another = booking(u, "20.00", paid=False, arrived=False)
    another.normalized_status = Reservation.Status.AWAITING_PAYMENT
    another.save()
    with pytest.raises(ValidationError):
        hold(u, "TEST-DOUBLE-SPEND", another.pk, "20.00")
    r.normalized_status = Reservation.Status.CONFIRMED
    r.save(update_fields=["normalized_status"])
    redeem(u, "TEST-REDEEM", r.pk)
    assert balance(m) == Decimal("2.50")
    assert redeem(u, "TEST-REDEEM-REPLAY", r.pk)["value"] == "20"
    assert restore_points(r.pk, "10.00", "TEST-REFUND-1") == Decimal("10.00")
    assert restore_points(r.pk, "10.00", "TEST-REFUND-REPLAY") == 0
    assert restore_points(r.pk, "20.00", "TEST-REFUND-2") == Decimal("10.00")
    with pytest.raises(ValidationError):
        restore_points(r.pk, "20.01", "TEST-REFUND-EXCESS")
    assert balance(m) == Decimal("22.50")
    release(u, "TEST-RELEASE", another.pk)


def test_full_points_stay_creates_no_new_points():
    u = user(1)
    m = join(u)
    reconcile(booking(u).pk)
    r = booking(u, "20.00", paid=False, arrived=False)
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save()
    hold(u, "TEST-FULL-HOLD", r.pk, "20.00")
    r.normalized_status = Reservation.Status.CONFIRMED
    r.save(update_fields=["normalized_status"])
    redeem(u, "TEST-FULL-REDEEM", r.pk)
    r.refresh_from_db()
    r.loyalty_state["arrival"] = {"at": timezone.now().isoformat(), "reference": "TEST"}
    r.save()
    assert reconcile(r.pk)["base"] == "0.00"
    assert balance(m) == Decimal("2.50")


def test_cancel_after_transfer_deferred_claim_only_from_future_rewards():
    u, v = user(1), user(2)
    m, target = join(u), join(v)
    r = booking(u)
    reconcile(r.pk)
    transfer(u, "TEST-SPENT", target.number, "22.50")
    r.refresh_from_db()
    # Full refund is justified here by same-day, documented departure before
    # 20:00; cancellation alone must not erase already consumed nights.
    r.loyalty_state["departure_settlement"] = {
        "consumed_cash_lodging": "0.00",
        "reference": "TEST-SAME-DAY-DEPARTURE",
    }
    r.normalized_status = Reservation.Status.CANCELLED
    r.confirmed_at, r.cancelled_at = None, timezone.now()
    r.save()
    assert reconcile(r.pk)["status"] == "settled"
    m.refresh_from_db()
    assert m.deferred_corrections[0]["remaining"] == "22.50"
    assert balance(m) == 0 and balance(target) == Decimal("22.50")
    transfer(v, "TEST-INCOMING-NOT-CLAWED", m.number, "5.00")
    assert balance(m) == Decimal("5.00")
    reconcile(booking(u, "100.00").pk)
    assert balance(m) == Decimal("5.00")
    m.refresh_from_db()
    assert Decimal(m.deferred_corrections[0]["remaining"]) == Decimal("19.50")


def test_actual_payment_domain_hook_then_arrival(django_capture_on_commit_callbacks):
    u = user(1)
    m = join(u)
    r = booking(u, arrived=False)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        HyperPayService._record_verified_booking_payment(
            r.booking_intent.payment_attempts.first(), r
        )
    assert callbacks and balance(m) == 0
    operator = get_user_model().objects.create_superuser(username="TEST-OPERATOR", password=None)
    confirm_arrival(operator, r.pk, arrived_at=timezone.now(), reference="TEST-ARRIVAL")
    assert balance(m) == Decimal("22.50")


def test_disabled_hook_has_no_callbacks_or_ledger_writes(django_capture_on_commit_callbacks):
    with override_settings(LOYALTY_ENABLED=False):
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            schedule_reconciliation("not-a-real-reservation")
        assert not callbacks
        assert reconcile("not-a-real-reservation")["status"] == "disabled"
        assert not Ledger.objects.exists()


def test_ledger_and_referral_append_only_and_unique_event():
    u = user(1)
    join(u)
    reconcile(booking(u).pk)
    row = Ledger.objects.first()
    with pytest.raises(ValidationError):
        row.save()
    with pytest.raises(ValidationError):
        Ledger.objects.all().update(credit=100)
    with pytest.raises(ValidationError):
        Referral.objects.all().delete()
    fields = {
        f.name: getattr(row, f.name)
        for f in Ledger._meta.fields
        if f.name
        not in {
            "id",
            "posted_at",
            "sequence",
            "operation_number",
            "operation_group",
        }
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        Ledger.objects.create(
            **fields, sequence=2, operation_number="LSA000002-01", operation_group="LSA000002"
        )


def test_guest_statement_never_contains_other_accounts():
    u, v = user(1), user(2)
    join(u)
    join(v)
    reconcile(booking(u).pk)
    reconcile(booking(v, "950.00").pk)
    result = statement(u)
    assert result["balance"] == Decimal("22.50")
    assert all(row.customer_id == u.pk for row in result["rows"])


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 7, 8, 9, 10, 20, 30])
@pytest.mark.parametrize(
    "base",
    ["0.00", "0.01", "0.99", "1.00", "600.00", "750.00", "850.00", "950.00", "999.99", "999999.99"],
)
def test_exact_reward_bounds_at_every_depth(base, depth):
    rows = reward_plan(base, 0, list(range(1, depth + 1)))
    value = cents(base)
    assert rows[0][3] == value * 3 // 100
    older = [r[3] for r in rows if r[1] == "indirect_referral"]
    assert all(n <= value // 100 for n in older)
    assert sum(older) <= value * 7 // 100
    assert sum(r[3] for r in rows) <= value * 15 // 100


def test_eight_elders_remainder_and_no_individual_roundup():
    rows = reward_plan("750.00", "owner", list(range(9)))
    assert [amount(r[3]) for r in rows[2:]] == [Decimal("6.57")] * 2 + [Decimal("6.56")] * 6
    assert reward_plan("999.99", "owner", [])[0][3] == 2999


@pytest.mark.parametrize(
    "hour,refundable", [(16, "750.00"), (19, "750.00"), (20, "450.00"), (21, "450.00")]
)
def test_early_departure_uses_property_local_time_and_actual_nightly_values(hour, refundable):
    nights = [
        {"date": "2026-10-10", "value": "250.00"},
        {"date": "2026-10-11", "value": "300.00"},
        {"date": "2026-10-12", "value": "450.00"},
    ]
    at = datetime(2026, 10, 11, hour - 3, tzinfo=UTC)
    assert departure_refund(nights, at, "Asia/Riyadh") == Decimal(refundable)
    assert refund_split(refundable, "600.00", "400.00")["cash"] == Decimal(refundable) * Decimal(
        "0.6"
    )


@pytest.mark.parametrize("value", [True, 1.2, "-1", "0.001", "NaN", "Infinity"])
def test_invalid_point_values_rejected(value):
    with pytest.raises(ValueError):
        cents(value)


def completed_change(r, value):
    old = r.total_price
    r.total_price = Decimal(value)
    r.normalized_status = Reservation.Status.MODIFIED
    r.save(update_fields=["total_price", "normalized_status"])
    return BookingModificationRequest.objects.create(
        reservation=r,
        request_type="change_dates",
        status="completed",
        old_check_in=r.check_in,
        old_check_out=r.check_out,
        new_check_in=r.check_in,
        new_check_out=r.check_out,
        old_guests=r.guests,
        new_guests=r.guests,
        old_total=old,
        new_total=Decimal(value),
        price_difference=Decimal(value) - old,
        currency="SAR",
        completed_at=timezone.now(),
        expires_at=timezone.now() + timedelta(hours=1),
        idempotency_key=f"TEST-CHANGE-{r.pk}-{value}",
        session_key_hash="TEST",
        quote_snapshot={
            "components": [{"type": "baseRate", "total": value, "included_in_total": True}]
        },
    )


def test_real_modification_records_recompute_both_guest_and_inviter(local_program):
    a, b = user(1), user(2)
    am, token = completed_inviter(a, local_program)
    bm = join(b, invitation_token=token, confirmed_inviter=am.pk)
    r = booking(b, "750.00")
    reconcile(r.pk)
    before = balance(am)
    completed_change(r, "600.00")
    assert reconcile(r.pk)["movements"] == 2
    assert balance(bm) == Decimal("18.00")
    assert balance(am) == before - Decimal("7.50")
    assert (
        Ledger.objects.filter(reservation=r, event_type="adjust", original__isnull=False).count()
        == 2
    )
    assert reconcile(r.pk)["movements"] == 0


def test_reextension_removes_deferred_claim_without_duplicating_reward(local_program):
    u, v = user(1), user(2)
    m, target = join(u), join(v)
    r = booking(u)
    reconcile(r.pk)
    transfer(u, "TEST-ALL-OUT", target.number, "22.50")
    completed_change(r, "600.00")
    reconcile(r.pk)
    m.refresh_from_db()
    assert Decimal(m.deferred_corrections[0]["remaining"]) == Decimal("4.50")
    local_program[0] += timedelta(minutes=1)
    completed_change(r, "750.00")
    reconcile(r.pk)
    m.refresh_from_db()
    assert not m.deferred_corrections and balance(m) == 0
    assert Ledger.objects.filter(reservation=r, event_type="earn").count() == 1


def test_mixed_checkout_sends_only_cash_and_freezes_held_points(monkeypatch):
    from apps.reservations.services.hostaway_booking import prepare_local_reservation

    u = user(1)
    join(u)
    reconcile(booking(u).pk)
    intent = payable_intent()
    intent.customer = u
    intent.save(update_fields=["customer"])
    r = prepare_local_reservation(intent)
    hold(u, "TEST-CHECKOUT-HOLD", r.pk, "20.00")
    stub = HyperPayStub()
    with override_settings(**HYPERPAY_SETTINGS):
        session = HyperPayService(client=stub).create_checkout(intent)
        again = HyperPayService(client=stub).create_checkout(intent)
    assert stub.payload["amount"] == "1230.00"
    assert session.attempt.pk == again.attempt.pk and stub.checkout_calls == 1
    intent.refresh_from_db()
    assert intent.total_price == Decimal("1250.00")
    assert intent.payment_amount_sar == Decimal("1250.00")
    assert cash_due(intent) == Decimal("1230.00")
    with pytest.raises(ValidationError):
        release(u, "TEST-CHECKOUT-NO-RELEASE", r.pk)
    session.attempt.status = PaymentAttempt.Status.FAILED
    session.attempt.save(update_fields=["status"])
    release(u, "TEST-CHECKOUT-RELEASE", r.pk)
    assert cash_due(intent) == Decimal("1250.00")


def test_full_points_booking_uses_inventory_service_not_card_receipt():
    u = user(1)
    m = join(u)
    reconcile(booking(u).pk)
    r = booking(u, "20.00", paid=False, arrived=False)
    r.booking_intent.payment_amount_sar = Decimal("20.00")
    r.booking_intent.save(update_fields=["payment_amount_sar"])
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save(update_fields=["normalized_status"])
    hold(u, "TEST-ONLY-POINTS-HOLD", r.pk, "20.00")
    calls = []

    def inventory(booking):
        calls.append(booking.pk)
        booking.normalized_status = Reservation.Status.CONFIRMED
        booking.is_test = True
        booking.save(update_fields=["normalized_status", "is_test"])
        return SimpleNamespace(code="confirmed")

    service = SimpleNamespace(create_points_reservation=inventory)
    assert confirm_points_booking(u, r.pk, booking_service=service)["status"] == "confirmed"
    assert confirm_points_booking(u, r.pk, booking_service=service)["status"] == "already_confirmed"
    assert len(calls) == 1 and balance(m) == Decimal("2.50")
    assert not PaymentAttempt.objects.filter(booking_intent=r.booking_intent).exists()


def test_cancel_before_arrival_automatically_restores_redeemed_points(
    django_capture_on_commit_callbacks,
):
    u = user(1)
    m = join(u)
    reconcile(booking(u).pk)
    r = booking(u, "20.00", paid=False, arrived=False)
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save()
    hold(u, "TEST-CANCEL-HOLD", r.pk, "20.00")
    r.normalized_status = Reservation.Status.CONFIRMED
    r.save(update_fields=["normalized_status"])
    redeem(u, "TEST-CANCEL-REDEEM", r.pk)
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CANCELLED
    r.save(update_fields=["normalized_status"])
    with django_capture_on_commit_callbacks(execute=True):
        schedule_reconciliation(r.pk)
    assert balance(m) == Decimal("22.50")
    with django_capture_on_commit_callbacks(execute=True):
        schedule_reconciliation(r.pk)
    assert Ledger.objects.filter(reservation=r, event_type="restore").count() == 1


def test_database_rejects_raw_history_edit():
    u = user(1)
    join(u)
    reconcile(booking(u).pk)
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE loyalty_ledger SET credit=999")


def test_full_points_uncertain_confirmation_cannot_dispatch_or_release_twice():
    u = user(1)
    join(u)
    reconcile(booking(u).pk)
    r = booking(u, "20.00", paid=False, arrived=False)
    r.booking_intent.payment_amount_sar = Decimal("20.00")
    r.booking_intent.save(update_fields=["payment_amount_sar"])
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save(update_fields=["normalized_status"])
    hold(u, "TEST-UNCERTAIN-HOLD", r.pk, "20.00")
    calls = []
    service = SimpleNamespace(
        create_points_reservation=lambda b: (
            calls.append(b.pk) or SimpleNamespace(code="request_uncertain")
        )
    )
    assert confirm_points_booking(u, r.pk, booking_service=service)["status"] == "request_uncertain"
    assert (
        confirm_points_booking(u, r.pk, booking_service=service)["status"]
        == "confirmation_in_progress"
    )
    with pytest.raises(ValidationError):
        release(u, "TEST-UNCERTAIN-RELEASE", r.pk)
    assert len(calls) == 1


def test_cancellation_without_departure_does_not_erase_consumed_rewards():
    u = user(1)
    m = join(u)
    r = booking(u)
    reconcile(r.pk)
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CANCELLED
    r.save(update_fields=["normalized_status"])
    assert reconcile(r.pk)["status"] == "departure_refund_evidence_required"
    assert balance(m) == Decimal("22.50")


@pytest.mark.parametrize("hour,retained", [(19, "7.50"), (20, "22.50")])
def test_native_departure_snapshot_preserves_consumed_nights(local_program, hour, retained):
    from apps.loyalty.stay import record_departure

    u = user(1)
    m = join(u)
    operator = user(9)
    operator.is_staff, operator.is_superuser = True, True
    operator.save()
    r = booking(u)
    reconcile(r.pk)
    r.refresh_from_db()
    r.loyalty_state["priced_nights"] = {
        "currency": "SAR",
        "reference": "TEST-BOUND-QUOTE",
        "nights": [
            {"date": r.check_in.isoformat(), "value": "250.00"},
            {"date": (r.check_in + timedelta(days=1)).isoformat(), "value": "500.00"},
        ],
    }
    r.save(update_fields=["loyalty_state"])
    local_program[0] += timedelta(days=1)
    at = local_program[0].replace(hour=hour - 3)
    local_program[0] = at
    result = record_departure(operator, r.pk, vacated_at=at, reference="TEST-EXIT")
    assert result["cash_refund"] == ("500.00" if hour == 19 else "0.00")
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CANCELLED
    r.save(update_fields=["normalized_status"])
    reconcile(r.pk)
    assert balance(m) == Decimal(retained)


def test_native_withdrawal_keeps_history_and_closes_completed_stay_account(local_program):
    u = user(1)
    m = join(u)
    r = booking(u)
    reconcile(r.pk)
    with pytest.raises(ValidationError):
        withdraw(u, "TEST-WITHDRAW-ACTIVE")
    local_program[0] += timedelta(days=3)
    result = withdraw(u, "TEST-WITHDRAW-COMPLETED")
    assert result["forfeited"] == "22.50" and balance(m) == 0
    assert withdraw(u, "TEST-WITHDRAW-COMPLETED") == result
    assert Ledger.objects.filter(member=m).count() == 2
    with pytest.raises(ValidationError):
        join(u)
