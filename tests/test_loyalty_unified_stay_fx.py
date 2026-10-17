"""Whole guest journeys, synthetic identities and frozen rates; no external writes."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.loyalty.accrual import SIGNED_TERMS_REFERENCE, SIGNED_TERMS_VERSION
from apps.loyalty.booking_operations import stay_controls
from apps.loyalty.checkout import cash_due, restore_cancelled_points
from apps.loyalty.models import Ledger
from apps.loyalty.payment_summary import for_attempt
from apps.loyalty.services import (
    available,
    balance,
    confirm_arrival,
    hold,
    issue_invite,
    reconcile,
    redeem,
)
from apps.loyalty.settlements import cancellation_plan, decrease_plan, record_refund_plan
from apps.loyalty.stay import departure_plan, record_departure
from apps.loyalty.valuation import intent_total_sar, to_sar, total_sar
from apps.payments.currency import CurrencyService, ExchangeRateSnapshot
from apps.payments.hyperpay.service import HyperPayService
from apps.reservations.models import BookingIntent, BookingModificationRequest, Reservation
from apps.reservations.services.modifications import ModificationService
from apps.reservations.services.owner_settlement import settlement_blocker, settlement_for
from tests.test_hyperpay import HYPERPAY_SETTINGS, HyperPayStub, own_intent
from tests.test_loyalty_stage8 import booking, join, payment, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def policy(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_UI_PREVIEW = settings.LOYALTY_PILOT_ONLY = False
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = SIGNED_TERMS_VERSION
    settings.LOYALTY_TERMS_REFERENCE = SIGNED_TERMS_REFERENCE
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = False
    settings.NOTIFICATIONS_ENABLED = settings.EMAIL_DELIVERY_ENABLED = False
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = False
    settings.BOOKING_CANCELLATION_REQUEST_ENABLED = True
    settings.BOOKING_AUTOMATIC_CANCELLATION_ENABLED = True
    clock = [datetime(2026, 10, 17, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    rates = ExchangeRateSnapshot(
        "synthetic-frozen", "SAR", {"SAR": Decimal(1), "MAD": Decimal("2.5")}, clock[0], clock[0]
    )
    monkeypatch.setattr(CurrencyService, "get_rates", lambda self: rates)
    monkeypatch.setattr(
        "httpx.Client.send", lambda *a, **k: pytest.fail("External network forbidden")
    )
    return clock


def administrator():
    return get_user_model().objects.create_superuser(username="unified-fixture-owner")


def contract(r, values, currency):
    q = r.booking_intent.quote
    q.night_prices = {
        "currency": currency,
        "reference": "synthetic-original-contract",
        "nights": [
            {"date": (r.check_in + timedelta(days=n)).isoformat(), "value": value}
            for n, value in enumerate(values)
        ],
    }
    q.save(update_fields=["night_prices"])


def foreign_stay(*, paid=True, member=None):
    guest = member.customer if member else user("foreign-journey")
    member = member or join(guest)
    r = booking(guest, "1875.00", paid=False, arrived=False)
    r.currency = "MAD"
    r.property.country_code = "MA"
    r.property.time_zone_name = "Africa/Casablanca"
    r.property.save()
    r.save()
    intent = r.booking_intent
    intent.currency = "MAD"
    rates = ExchangeRateSnapshot(
        "synthetic-frozen",
        "SAR",
        {"SAR": Decimal(1), "MAD": Decimal("2.5")},
        timezone.now(),
        timezone.now(),
    )
    with CurrencyService() as service, patch.object(service, "get_rates", return_value=rates):
        quote = service.create_quote(
            source_amount=intent.total_price,
            source_currency="MAD",
            display_currency="SAR",
            quote_created_at=intent.created_at,
            quote_expires_at=intent.expires_at,
        )
    intent.payment_amount_sar = quote.payment_amount_sar
    intent.exchange_rate_snapshot = dict(quote.snapshot)
    intent.save()
    intent.quote.currency = "MAD"
    intent.quote.save()
    contract(r, ["1000.00", "875.00"], "MAD")
    if paid:
        payment(r, Decimal("750.00"))
    return r, member


@pytest.mark.parametrize("hour,refund", [(19, "350.00"), (20, "0.00")])
def test_timed_admission_end_settlement_and_cancellation_are_one_journey(policy, hour, refund):
    guest = user("automatic-complete")
    member = join(guest)
    r = booking(guest, arrived=False)
    contract(r, ["400.00", "350.00"], "SAR")
    assert reconcile(r.pk)["movements"] == 1
    r.refresh_from_db()
    operator = administrator()
    assert "arrival" not in r.loyalty_state and stay_controls(operator, r)["can_end"]
    policy[0] = (policy[0] + timedelta(days=1)).replace(hour=hour - 3)
    with pytest.raises(ValidationError):
        cancellation_plan(r)
    outcome = ModificationService.create_cancellation_request(
        r, session_hash=r.booking_intent.session_key_hash
    )
    assert outcome.code == "loyalty_departure_evidence_required"
    result = record_departure(operator, r.pk, vacated_at=policy[0], reference="synthetic-departure")
    assert result["cash_refund"] == refund
    r.refresh_from_db()
    assert cancellation_plan(r)["cash"] == Decimal(refund)
    r.normalized_status = Reservation.Status.CANCELLED
    r.save()
    assert reconcile(r.pk)["status"] == "settled"
    assert balance(member) == (Decimal("12.00") if hour == 19 else Decimal("22.50"))
    assert Ledger.objects.filter(event_type="earn").count() == 1


def test_completed_timed_stay_unlocks_invitation_without_manual_arrival(policy):
    guest = user("completed-without-reception")
    member = join(guest)
    r = booking(guest, arrived=False)
    reconcile(r.pk)
    with pytest.raises(ValidationError):
        issue_invite(guest, r.pk)
    policy[0] += timedelta(days=3)
    client = Client()
    client.force_login(guest)
    page = client.get("/my-bookings/rewards/")
    assert r.pk in [item.pk for item in page.context["loyalty_completed"]]
    assert issue_invite(guest, r.pk)
    r.refresh_from_db()
    assert "arrival" not in r.loyalty_state
    assert balance(member) == Decimal("22.50") and Ledger.objects.count() == 1
    assert departure_plan(r, vacated_at=policy[0])["normal"]


def test_before_three_cancellation_stays_full_and_no_admission_is_fabricated(policy):
    policy[0] = policy[0].replace(hour=11)
    guest = user("before-three")
    join(guest)
    r = booking(guest, arrived=False)
    assert cancellation_plan(r)["cash"] == Decimal("750.00")
    assert not stay_controls(administrator(), r)["can_end"]
    assert reconcile(r.pk)["status"] == "arrival_required"


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_foreign_partial_checkout_collects_only_sar_remainder_and_keeps_source_total(
    policy, language
):
    guest = user("funded-member")
    member = join(guest)
    reconcile(booking(guest, "10000.00").pk)
    r, _ = foreign_stay(paid=False, member=member)
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save()
    intent = r.booking_intent
    intent.status, intent.language = BookingIntent.Status.AWAITING_PAYMENT, language
    intent.billing_street1 = "Synthetic street"
    intent.billing_city = intent.billing_state = "Riyadh"
    intent.billing_country, intent.billing_postcode = "SA", "12345"
    intent.save()
    client = Client()
    client.force_login(guest)
    page = client.get(reverse("reservations:intent_detail", args=[intent.public_reference]))
    assert page.context["points_checkout"]["cash"] == Decimal("750.00")
    assert (
        client.post(
            reverse("reservations:booking_points", args=[intent.public_reference]),
            {
                "action": "apply",
                "points": "200.00",
                "request_reference": "synthetic-foreign-allocation",
            },
        ).status_code
        == 302
    )
    assert cash_due(intent) == Decimal("550.00") and available(member) == Decimal("100.00")
    with override_settings(**HYPERPAY_SETTINGS):
        stub = HyperPayStub()
        session = HyperPayService(client=stub).create_checkout(intent)
    assert session.attempt.amount == Decimal("550.00")
    assert for_attempt(session.attempt)["gross"] == Decimal("750.00")
    assert intent.currency == "MAD" and intent.total_price == Decimal("1875.00")
    session.attempt.status = session.attempt.Status.SUCCEEDED
    session.attempt.verified_at = policy[0]
    session.attempt.provider_payment_id = "synthetic-foreign-capture"
    session.attempt.save()
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CONFIRMED
    r.save()
    redeem(guest, "synthetic-foreign-confirm", r.pk)
    # Morocco local 15:00 is later than Riyadh 15:00 in this fixture.
    policy[0] += timedelta(hours=2)
    assert reconcile(r.pk)["base"] == "550.00"
    assert Ledger.objects.filter(reservation=r, event_type="earn").get().credit == Decimal("16.50")


@pytest.mark.parametrize("hour,refund", [(19, "350.00"), (20, "0.00")])
def test_foreign_early_departure_uses_source_nights_and_frozen_sar_rate(policy, hour, refund):
    policy[0] += timedelta(hours=2)
    r, member = foreign_stay()
    assert reconcile(r.pk)["base"] == "750.00"
    r.refresh_from_db()
    policy[0] = datetime.combine(
        r.check_in + timedelta(days=1), time(hour), ZoneInfo(r.property.time_zone_name)
    ).astimezone(UTC)
    result = record_departure(
        administrator(), r.pk, vacated_at=policy[0], reference="synthetic-foreign-exit"
    )
    assert result["cash_refund"] == refund
    assert result["settlement_currency"] == "SAR"
    assert "arrival" not in r.loyalty_state
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CANCELLED
    r.save()
    assert reconcile(r.pk)["status"] == "settled"
    assert balance(member) == (Decimal("12.00") if hour == 19 else Decimal("22.50"))


def test_foreign_decrease_uses_frozen_rate_and_original_sar_receipts(policy):
    policy[0] += timedelta(hours=2)
    r, member = foreign_stay()
    reconcile(r.pk)
    r.refresh_from_db()
    assert not settlement_blocker(r)
    assert settlement_for(r, Decimal("1250.00")).refund == Decimal("625.00")
    plan = decrease_plan(r, Decimal("1250.00"))
    assert plan["cash"] == Decimal("250.00")
    change = BookingModificationRequest.objects.create(
        reservation=r,
        request_type="change_dates",
        status="completed",
        old_check_in=r.check_in,
        old_check_out=r.check_out,
        new_check_in=r.check_in,
        new_check_out=r.check_out,
        old_total=r.total_price,
        new_total=Decimal("1250.00"),
        old_guests=r.guests,
        new_guests=r.guests,
        price_difference=Decimal("-625.00"),
        currency="MAD",
        idempotency_key="synthetic-foreign-decrease",
        session_key_hash="synthetic",
        expires_at=policy[0] + timedelta(hours=1),
        completed_at=policy[0],
        quote_snapshot={
            "components": [{"type": "baseRate", "total": "1250.00", "included_in_total": True}]
        },
    )
    r.total_price = change.new_total
    r.save()
    assert reconcile(r.pk)["base"] == "500.00"
    assert balance(member) == Decimal("15.00")
    rows = record_refund_plan(change, plan, "modification_decrease")
    assert len(rows) == 1 and rows[0].currency == "SAR" and rows[0].amount == Decimal("250.00")
    assert total_sar(r) == Decimal("500.00")
    assert to_sar(r, Decimal("250.00")) == Decimal("100.00")


def test_foreign_increase_keeps_its_own_funding_snapshot_not_todays_rate(policy):
    policy[0] += timedelta(hours=2)
    r, member = foreign_stay()
    reconcile(r.pk)
    r.refresh_from_db()
    rates = ExchangeRateSnapshot(
        "synthetic-extension",
        "SAR",
        {"SAR": Decimal(1), "MAD": Decimal("3")},
        policy[0],
        policy[0],
    )
    with CurrencyService() as service, patch.object(service, "get_rates", return_value=rates):
        fx = service.create_quote(
            source_amount=Decimal("600.00"),
            source_currency="MAD",
            display_currency="SAR",
            quote_created_at=policy[0],
            quote_expires_at=policy[0] + timedelta(hours=1),
        )
    change = BookingModificationRequest.objects.create(
        reservation=r,
        request_type="extend_stay",
        status="completed",
        old_check_in=r.check_in,
        old_check_out=r.check_out,
        new_check_in=r.check_in,
        new_check_out=r.check_out + timedelta(days=1),
        old_total=r.total_price,
        new_total=Decimal("2475.00"),
        old_guests=r.guests,
        new_guests=r.guests,
        price_difference=Decimal("600.00"),
        payment_amount_sar=fx.payment_amount_sar,
        currency="MAD",
        idempotency_key="synthetic-foreign-extension",
        session_key_hash="synthetic",
        expires_at=policy[0] + timedelta(hours=1),
        completed_at=policy[0],
        quote_snapshot={
            "fx": dict(fx.snapshot),
            "components": [{"type": "baseRate", "total": "2475.00", "included_in_total": True}],
        },
    )
    receipt = payment(r, Decimal("200.00"))
    receipt.modification_request = change
    receipt.save()
    r.total_price = change.new_total
    r.check_out = change.new_check_out
    r.nights += 1
    r.save()
    assert total_sar(r) == Decimal("950.00")  # 750 original + 200 extension, not 990 or 825.
    assert reconcile(r.pk)["base"] == "950.00"
    assert balance(member) == Decimal("28.50")
    assert decrease_plan(r, Decimal("1980.00"))["cash"] == Decimal("190.00")


def test_foreign_full_points_stay_has_no_fake_card_receipt_and_cannot_earn_again(policy):
    guest = user("full-points-foreign")
    member = join(guest)
    reconcile(booking(guest, "30000.00").pk)
    r, _ = foreign_stay(paid=False, member=member)
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save()
    hold(guest, "synthetic-full-fx-hold", r.pk, "750.00")
    assert cash_due(r.booking_intent) == 0
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CONFIRMED
    r.save()
    redeem(guest, "synthetic-full-fx-redeem", r.pk)
    policy[0] += timedelta(hours=2)
    assert reconcile(r.pk)["base"] == "0.00"
    assert not r.booking_intent.payment_attempts.exists()
    assert balance(member) == Decimal("150.00")


def test_tampered_or_missing_foreign_snapshot_cannot_reserve_points(policy):
    r, member = foreign_stay(paid=False)
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save()
    r.booking_intent.exchange_rate_snapshot = {}
    r.booking_intent.save()
    with pytest.raises(ValidationError):
        intent_total_sar(r.booking_intent)
    with pytest.raises(ValidationError):
        total_sar(r)
    with pytest.raises(ValidationError):
        hold(member.customer, "synthetic-bad-snapshot", r.pk, "1.00")
    r.refresh_from_db()
    assert not r.loyalty_state.get("hold")


def test_foreign_guest_can_link_checkout_without_starting_in_rewards(policy):
    r, member = foreign_stay(paid=False)
    intent = r.booking_intent
    intent.customer = None
    intent.status = BookingIntent.Status.AWAITING_PAYMENT
    intent.guest_email = member.customer.email
    intent.save()
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save()
    client = Client()
    own_intent(client, intent)
    page = client.get(reverse("reservations:intent_detail", args=[intent.public_reference]))
    assert "checkout_account" in page.context
    client.force_login(member.customer)
    response = client.post(
        reverse("reservations:booking_account", args=[intent.public_reference]), follow=True
    )
    intent.refresh_from_db()
    assert intent.customer_id == member.customer_id
    assert response.context["points_checkout"]["cash"] == Decimal("750.00")


def test_later_staff_arrival_cannot_move_an_earlier_timed_admission(policy):
    from apps.loyalty.accrual import effective_arrival

    guest = user("timed-then-staff")
    member = join(guest)
    r = booking(guest, arrived=False)
    reconcile(r.pk)
    r.refresh_from_db()
    original = effective_arrival(r)["at"]
    policy[0] += timedelta(hours=2)
    assert (
        confirm_arrival(
            administrator(), r.pk, arrived_at=policy[0], reference="synthetic-reception"
        )["movements"]
        == 0
    )
    r.refresh_from_db()
    assert effective_arrival(r)["at"] == original
    assert balance(member) == Decimal("22.50")


def test_foreign_mixed_departure_returns_points_and_sar_cash_without_double_credit(policy):
    guest = user("mixed-foreign-refund")
    member = join(guest)
    reconcile(booking(guest, "10000.00").pk)
    r, _ = foreign_stay(paid=False, member=member)
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save()
    hold(guest, "synthetic-mixed-hold", r.pk, "200.00")
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CONFIRMED
    r.save()
    payment(r, Decimal("550.00"))
    redeem(guest, "synthetic-mixed-redeem", r.pk)
    policy[0] += timedelta(hours=2)
    reconcile(r.pk)
    r.refresh_from_db()
    policy[0] = datetime.combine(
        r.check_in + timedelta(days=1), time(19), ZoneInfo(r.property.time_zone_name)
    ).astimezone(UTC)
    plan = record_departure(
        administrator(), r.pk, vacated_at=policy[0], reference="synthetic-mixed-exit"
    )
    assert plan["cash_refund"] == "256.66" and plan["points_refund"] == "93.34"
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CANCELLED
    r.save()
    assert restore_cancelled_points(r.pk) == Decimal("93.34")
    assert restore_cancelled_points(r.pk) == 0
    assert reconcile(r.pk)["base"] == "293.34"
    assert balance(member) == Decimal("202.14")
