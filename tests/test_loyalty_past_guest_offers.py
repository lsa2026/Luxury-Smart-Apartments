"""Synthetic owner offers: real financial rules, no network or historical campaign."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.loyalty import booking_invitations as invitations
from apps.loyalty.models import Ledger, Member
from apps.loyalty.past_guest_invitation_copy import invitation_caption
from apps.loyalty.past_guest_offers import KEY, SALT, apply_for_user, quote
from apps.loyalty.phone_gifts import phone_hash
from apps.loyalty.services import balance, reconcile
from apps.payments.models import PaymentAttempt
from apps.reservations.models import ManualBookingDraft, Reservation
from tests import test_loyalty_booking_invitations as manual
from tests.test_loyalty_stage8 import join, user

pytestmark = pytest.mark.django_db
rig = manual.rig
PHONE = "+12025550123"


def send(rig, booking, *, uncertain=False):
    client, _, provider, _ = rig
    if uncertain:
        provider.send_image.side_effect = TimeoutError()
    response = client.post(manual.url(booking), manual.payload(client, booking))
    assert response.status_code == 302
    manual.deliver_latest(booking)
    booking.refresh_from_db()
    assert booking.loyalty_state[KEY]


@pytest.mark.parametrize("linked", [True, False])
@pytest.mark.parametrize("uncertain", [True, False])
def test_offer_credits_exactly_once_after_join_and_matching_phone(rig, linked, uncertain):
    booking = manual.past_booking()
    guest = user("past-offer")
    if linked:
        booking.booking_intent.customer = guest
        booking.booking_intent.save()
    send(rig, booking, uncertain=uncertain)
    assert not Ledger.objects.exists() and not Member.objects.exists()
    VerifiedPhone.objects.create(user=guest, phone=PHONE)
    member = join(guest)
    assert balance(member) == Decimal("15.00")
    apply_for_user(guest)
    assert reconcile(booking.pk)["movements"] == 0
    assert join(guest).pk == member.pk
    assert Ledger.objects.filter(reservation=booking, event_type="earn").count() == 1
    row = Ledger.objects.get(reservation=booking)
    assert row.evidence[KEY]["quote"]["reward"] == "15.00"
    assert row.actual_arrival_at is None  # no invented physical arrival
    assert row.evidence["terms_version"] == member.terms_version
    assert not row.direct_inviter_id
    assert quote_blocked(booking)
    if not linked:
        booking.booking_intent.refresh_from_db()
        assert booking.booking_intent.customer_id is None


def quote_blocked(booking):
    try:
        quote(booking)
    except ValidationError:
        return True
    return False


def test_offer_uses_persisted_financial_snapshot_despite_stale_model_values(rig):
    booking = manual.past_booking()
    expected = quote(booking)
    booking.total_price = Decimal("9999.00")
    booking.booking_intent.total_price = Decimal("9999.00")
    assert quote(booking) == expected
    booking.refresh_from_db()
    assert quote(booking) == expected


def final_price_booking(rig, component_type="accommodation"):
    booking = manual.past_booking()
    booking.total_price = Decimal("600.00")
    booking.save()
    intent = booking.booking_intent
    intent.total_price = intent.payment_amount_sar = Decimal("600.00")
    intent.save()
    original = intent.quote
    original.total_price = Decimal("700.00")
    original.components = [{"type": component_type, "total": "700", "included_in_total": True}]
    original.save()
    PaymentAttempt.objects.filter(booking_intent=intent).update(amount=Decimal("600.00"))
    draft = ManualBookingDraft.objects.create(
        quote=original,
        property=booking.property,
        check_in=booking.check_in,
        check_out=booking.check_out,
        nights=booking.nights,
        guests=booking.guests,
        currency="SAR",
        system_total_price=Decimal("700.00"),
        final_total_price=Decimal("600.00"),
        payment_amount_sar=Decimal("600.00"),
        price_source=ManualBookingDraft.PriceSource.MANUAL_OVERRIDE,
        status=ManualBookingDraft.Status.BOOKED_AWAITING_PAYMENT,
        availability_checked_at=timezone.now(),
        expires_at=timezone.now() + timedelta(minutes=10),
        created_by=rig[1],
    )
    return booking, draft


@pytest.mark.parametrize("component_type", ["accommodation", "baseRate"])
@pytest.mark.parametrize("channel", ["whatsapp", "email"])
def test_owner_final_hyperpay_price_drives_preview_and_once_only_award(
    rig, component_type, channel
):
    booking, draft = final_price_booking(rig, component_type)
    payment_before = list(PaymentAttempt.objects.values())
    offer = quote(booking)
    assert Decimal(offer["total"]) == Decimal("600.00")
    assert Decimal(offer["base"]) == Decimal("600.00")
    assert offer["reward"] == "18.00"
    client = rig[0]
    context = next(
        row
        for row in client.get(manual.url(booking)).context["loyalty_invitation"]["channels"]
        if row["channel"] == channel
    )
    assert context["confirmation"] and "600.00" in context["preview_text"]
    assert "18.00" in context["preview_text"] and "700.00" not in context["preview_text"]
    assert not Ledger.objects.exists() and not Member.objects.exists()
    client.post(manual.url(booking), manual.payload(client, booking, channel))
    manual.deliver_latest(booking, channel)
    guest = user("owner-final-price")
    VerifiedPhone.objects.create(user=guest, phone=PHONE)
    member = join(guest)
    booking.refresh_from_db()
    assert balance(member) == Decimal("18.00"), booking.loyalty_state
    apply_for_user(guest)
    assert Ledger.objects.filter(reservation=booking).count() == 1
    assert Ledger.objects.get(reservation=booking).evidence["owner_final_price"]["draft"] == str(
        draft.pk
    )
    draft.quote.refresh_from_db()
    assert draft.quote.total_price == Decimal("700.00")
    assert draft.quote.components[0]["total"] == "700"
    assert list(PaymentAttempt.objects.values()) == payment_before


@pytest.mark.parametrize(
    "reason",
    [
        "fees",
        "partial",
        "overpaid",
        "manual_receipt",
        "unverified",
        "draft_total",
        "draft_dates",
        "draft_not_booked",
        "draft_missing",
        "test_payment",
    ],
)
def test_final_price_adapter_never_guesses_fees_or_accepts_unmatched_receipts(rig, reason):
    booking, draft = final_price_booking(rig)
    payments = PaymentAttempt.objects.filter(booking_intent=booking.booking_intent)
    if reason == "fees":
        draft.quote.components = [
            {"type": "baseRate", "total": "650", "included_in_total": True},
            {"type": "cleaningFee", "total": "50", "included_in_total": True},
        ]
        draft.quote.save()
    elif reason == "partial":
        payments.update(amount=Decimal("599.00"))
    elif reason == "overpaid":
        payments.update(amount=Decimal("700.00"))
    elif reason == "manual_receipt":
        payments.update(provider="manual")
    elif reason == "unverified":
        payments.update(verified_at=None)
    elif reason == "test_payment":
        payments.update(provider_result_code="000.100.112")
    elif reason == "draft_total":
        draft.final_total_price = Decimal("700.00")
        draft.save()
    elif reason == "draft_dates":
        draft.check_in -= timedelta(days=1)
        draft.save()
    elif reason == "draft_not_booked":
        draft.status = ManualBookingDraft.Status.QUOTED
        draft.save()
    elif reason == "draft_missing":
        draft.delete()
    assert quote_blocked(booking)
    assert not rig[2].send_image.called and not Ledger.objects.exists()


@pytest.mark.parametrize(
    "reason", ["no_offer", "queued", "queue_failed", "wrong_phone", "tampered"]
)
def test_no_past_award_without_delivered_offer_and_verified_target(rig, reason):
    booking = manual.past_booking()
    guest = user("not-authorized")
    booking.booking_intent.customer = guest
    booking.booking_intent.save()
    if reason in {"wrong_phone", "tampered"}:
        send(rig, booking)
        if reason == "tampered":
            booking.loyalty_state[KEY]["proof"] += "bad"
            booking.save()
    elif reason != "no_offer":
        client = rig[0]
        client.post(manual.url(booking), manual.payload(client, booking))
        booking.refresh_from_db()
        if reason == "queue_failed":
            invitations._finish(
                booking.pk,
                "queue_failed",
                expected="queued",
                request_id=booking.loyalty_state[invitations.LATEST],
            )
    VerifiedPhone.objects.create(
        user=guest, phone=PHONE if reason != "wrong_phone" else "+12025550124"
    )
    member = join(guest)
    apply_for_user(guest)
    assert balance(member) == 0 and not Ledger.objects.exists()


def test_email_join_waits_for_phone_verification(rig, django_capture_on_commit_callbacks):
    booking = manual.past_booking()
    guest = user("phone-after-join")
    booking.booking_intent.customer = guest
    booking.booking_intent.save()
    send(rig, booking)
    member = join(guest)
    assert balance(member) == 0
    with django_capture_on_commit_callbacks(execute=True):
        VerifiedPhone.objects.create(user=guest, phone=PHONE)
    assert balance(member) == Decimal("15.00")
    assert Ledger.objects.count() == 1


@pytest.mark.parametrize(
    "kind",
    [
        "future",
        "current",
        "cancelled",
        "external",
        "unpaid",
        "partial",
        "missing_breakdown",
        "missing_zone",
    ],
)
def test_only_past_eligible_verified_paid_stays_can_be_invited(rig, kind):
    booking = manual.past_booking()
    if kind == "future":
        booking.check_in = timezone.now().date() + timedelta(days=3)
        booking.check_out = booking.check_in + timedelta(days=1)
    elif kind == "current":
        booking.check_out = timezone.now().date() + timedelta(days=1)
    elif kind == "cancelled":
        booking.normalized_status = Reservation.Status.CANCELLED
    elif kind == "external":
        booking.source_type = Reservation.SourceType.EXTERNAL_CHANNEL
    elif kind == "unpaid":
        PaymentAttempt.objects.filter(booking_intent=booking.booking_intent).update(
            verified_at=None
        )
    elif kind == "partial":
        PaymentAttempt.objects.filter(booking_intent=booking.booking_intent).update(
            amount=Decimal("100")
        )
    elif kind == "missing_breakdown":
        q = booking.booking_intent.quote
        q.components = []
        q.save()
    elif kind == "missing_zone":
        booking.property.time_zone_name = ""
        booking.property.save()
    booking.save()
    assert quote_blocked(booking)
    page = rig[0].get(manual.url(booking))
    assert not any(
        row.get("confirmation") for row in page.context["loyalty_invitation"]["channels"]
    )
    assert not rig[2].send_image.called and not Ledger.objects.exists()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_amounts_exclude_fees_and_round_down_without_fake_values(rig, language):
    booking = manual.past_booking()
    q = booking.booking_intent.quote
    q.components = [
        {"type": "baseRate", "total": "449.99", "included_in_total": True},
        {"type": "cleaningFee", "total": "50.01", "included_in_total": True},
    ]
    q.save()
    reward = quote(booking)
    assert (
        Decimal(reward["total"]) == Decimal("500.00")
        and reward["base"] == "449.99"
        and reward["reward"] == "13.49"
    )
    caption = invitation_caption(
        language, "https://example.invalid/my-bookings/rewards/", "Test Guest", reward
    )
    assert all(value in caption for value in ["500.00", "449.99", "13.49"])
    assert len(caption.encode("utf-16-le")) // 2 <= 1024
    assert "بأثر رجعي" not in caption and "external-platform" not in caption


def test_changed_financial_snapshot_cannot_send_or_credit_another_amount(rig):
    booking = manual.past_booking()
    client = rig[0]
    request = manual.payload(client, booking)
    q = booking.booking_intent.quote
    q.components = [
        {"type": "baseRate", "total": "400.00", "included_in_total": True},
        {"type": "cleaningFee", "total": "100.00", "included_in_total": True},
    ]
    q.save()
    client.post(manual.url(booking), request)
    booking.refresh_from_db()
    assert not booking.loyalty_state.get(KEY)
    assert not rig[2].send_image.called and not Ledger.objects.exists()


def test_changed_financial_facts_after_delivery_require_review_not_another_reward(rig):
    booking = manual.past_booking()
    guest = user("changed-after-offer")
    send(rig, booking)
    q = booking.booking_intent.quote
    q.components = [
        {"type": "baseRate", "total": "400", "included_in_total": True},
        {"type": "cleaningFee", "total": "100", "included_in_total": True},
    ]
    q.save()
    VerifiedPhone.objects.create(user=guest, phone=PHONE)
    member = join(guest)
    assert balance(member) == 0 and not Ledger.objects.exists()
    booking.booking_intent.refresh_from_db()
    assert booking.booking_intent.customer_id is None


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_redemption_cannot_double_credit(rig):
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL advisory locks.")
    booking = manual.past_booking()
    guest = user("concurrent-past-offer")
    member = join(guest)
    # No offer was present at enrollment; exercise the post-delivery/verification hook concurrently.
    VerifiedPhone.objects.create(user=guest, phone=PHONE)
    booking.booking_intent.customer = guest
    booking.booking_intent.save()
    reward = quote(booking)
    request_id = "synthetic-owner-offer"
    booking.loyalty_state[KEY] = {
        "phone": phone_hash(PHONE),
        "proof": signing.dumps(
            {
                "booking": str(booking.pk),
                "phone": __import__("apps.loyalty.phone_gifts", fromlist=["phone_hash"]).phone_hash(
                    PHONE
                ),
                "customer": guest.pk,
                "quote": reward,
                "operator": rig[1].pk,
                "request_id": request_id,
                "channel": "whatsapp",
                "issued_at": timezone.now().isoformat(),
            },
            salt=SALT,
        ),
    }
    booking.loyalty_state[invitations.REQUESTS] = {
        request_id: {"status": "accepted", "operator": rig[1].pk}
    }
    booking.save()
    barrier = Barrier(2)

    def run(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            apply_for_user(guest)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    booking.refresh_from_db()
    assert Ledger.objects.filter(reservation=booking).count() == 1, booking.loyalty_state
    assert balance(member) == Decimal("15.00")


def test_existing_award_adjusts_down_and_never_exceeds_the_owner_offer(rig):
    booking = manual.past_booking()
    guest = user("past-adjustment")
    booking.booking_intent.customer = guest
    booking.booking_intent.save()
    send(rig, booking)
    binding = VerifiedPhone.objects.create(user=guest, phone=PHONE)
    member = join(guest)
    assert balance(member) == Decimal("15.00")
    booking.refresh_from_db()
    # An approved identity correction retains the earned reward's beneficiary.
    binding.phone = "+12025550124"
    binding.save()
    for value, expected in [("400.00", "12.00"), ("600.00", "15.00")]:
        intent = booking.booking_intent
        intent.total_price = Decimal(value)
        intent.save()
        q = intent.quote
        q.total_price = Decimal(value)
        q.components = [{"type": "baseRate", "total": value, "included_in_total": True}]
        q.save()
        booking.total_price = Decimal(value)
        booking.save()
        if value == "600.00":
            from tests.test_loyalty_stage8 import payment

            payment(booking, Decimal("100.00"))
        reconcile(booking.pk)
        booking.refresh_from_db()
        assert balance(member) == Decimal(expected)
    assert Ledger.objects.filter(reservation=booking, event_type="earn").count() == 1


def test_past_offer_never_grants_retroactive_referral_rewards(rig, monkeypatch):
    from apps.loyalty import services

    parent = join(user("past-parent"))
    parent.invitation_token = "synthetic-past-parent"
    parent.save(update_fields=["invitation_token"])
    monkeypatch.setattr(services, "invitation_eligible", lambda member: True)
    booking = manual.past_booking()
    guest = user("past-referred")
    send(rig, booking)
    VerifiedPhone.objects.create(user=guest, phone=PHONE)
    member = join(guest, invitation_token=parent.invitation_token, confirmed_inviter=parent.pk)
    assert balance(member) == Decimal("15.00")
    assert balance(parent) == 0
    assert not Ledger.objects.filter(reservation=booking).exclude(member=member).exists()


def test_email_invitation_uses_the_same_phone_bound_offer(rig, mailoutbox):
    booking = manual.past_booking()
    client = rig[0]
    client.post(manual.url(booking), manual.payload(client, booking, "email"))
    manual.deliver_latest(booking, "email")
    assert len(mailoutbox) == 1 and "15.00" in mailoutbox[0].body
    guest = user("past-email-offer")
    VerifiedPhone.objects.create(user=guest, phone=PHONE)
    member = join(guest)
    assert balance(member) == Decimal("15.00")
    assert Ledger.objects.filter(reservation=booking).count() == 1


@pytest.mark.parametrize("change", ["future", "cancelled", "unpaid"])
def test_worker_rechecks_past_stay_eligibility_after_owner_queue(rig, change):
    booking = manual.past_booking()
    rig[0].post(manual.url(booking), manual.payload(rig[0], booking))
    booking.refresh_from_db()
    if change == "future":
        booking.check_out = timezone.now().date() + timedelta(days=2)
    elif change == "cancelled":
        booking.normalized_status = Reservation.Status.CANCELLED
    else:
        PaymentAttempt.objects.filter(booking_intent=booking.booking_intent).update(
            verified_at=None
        )
    booking.save()
    manual.deliver_latest(booking)
    booking.refresh_from_db()
    assert manual.whatsapp_row(booking)["status"] == "blocked"
    assert not rig[2].send_image.called and not Ledger.objects.exists()
