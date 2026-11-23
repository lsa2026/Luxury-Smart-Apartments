"""Synthetic owner offers with independently recorded workgroup receipts."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.loyalty.models import JoinInvitation, Ledger
from apps.loyalty.past_guest_offers import KEY, apply_for_user, authorized, retain_sent_offer
from apps.loyalty.services import balance
from apps.reservations.telegram_stay_models import TelegramWebsiteHandoff
from apps.reservations.telegram_stay_render import sha
from tests import test_loyalty_booking_invitations as manual
from tests.test_loyalty_stage8 import join, user

pytestmark = pytest.mark.django_db
rig = manual.rig
PHONE = "+12025550123"


def offer(rig, *, state="handed_off", journal=True):
    client = rig[0]
    booking = manual.past_booking()
    assert client.post(manual.url(booking), manual.payload(client, booking)).status_code == 302
    booking.refresh_from_db()
    rows = booking.loyalty_state["owner_join_invitation_requests"]
    key = next(reversed(rows))
    rows[key].update(status=state, provider_id="321")
    booking.save(update_fields=["loyalty_state"])
    # Synthetic completed preflight for the manual workgroup channel.
    JoinInvitation.objects.filter(pk=rows[key]["shared_invitation_id"]).update(
        channel="workgroup_manual", dispatched_at=timezone.now(), state="unknown"
    )
    source = f"{booking.pk}:{key}"
    if journal:
        TelegramWebsiteHandoff.objects.create(
            source_kind="loyalty_invitation",
            source_id=source,
            job_key="site01wg:loyalty_invitation:" + sha(source),
            reference_id="synthetic:" + str(uuid.uuid4()),
            source_revision="a" * 64,
            phone_hash="b" * 64,
            content_sha256="c" * 64,
            asset_sha256="d" * 64,
            state="handed_off",
            attempt_id=uuid.uuid4(),
            provider_message_id="321",
            provider_message_ids=["321"],
            accepted_at=timezone.now(),
            not_before=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=15),
        )
    return booking, rows[key]


def test_handoff_offer_requires_phone_then_credits_once_without_guest_acceptance(rig):
    booking, row = offer(rig)
    guest = user("group-offer")
    member = join(guest)
    assert authorized(booking) is None and not Ledger.objects.exists()
    VerifiedPhone.objects.create(user=guest, phone=PHONE)
    apply_for_user(guest)
    apply_for_user(guest)
    assert balance(member) == Decimal("15.00")
    assert Ledger.objects.filter(reservation=booking).count() == 1
    booking.refresh_from_db()
    assert (
        next(iter(booking.loyalty_state["owner_join_invitation_requests"].values()))["status"]
        == "handed_off"
    )
    assert "accepted_at" not in row


@pytest.mark.parametrize(
    "state,journal",
    [("queued", True), ("handoff_wait", True), ("handoff_hold", True), ("handed_off", False)],
)
def test_queued_unknown_or_unproven_staff_copy_cannot_award(rig, state, journal):
    booking, _ = offer(rig, state=state, journal=journal)
    guest = user("unproven-group-offer")
    VerifiedPhone.objects.create(user=guest, phone=PHONE)
    member = join(guest)
    assert authorized(booking) is None
    apply_for_user(guest)
    assert balance(member) == 0 and not Ledger.objects.exists()


def test_wrong_phone_cannot_claim_handed_off_offer(rig):
    booking, _ = offer(rig)
    guest = user("wrong-group-phone")
    VerifiedPhone.objects.create(user=guest, phone="+12025550199")
    member = join(guest)
    apply_for_user(guest)
    assert authorized(booking) is None and balance(member) == 0
    assert not Ledger.objects.exists()


def test_repeat_retains_same_owner_offer_and_requires_matching_receipt(rig):
    from django.core import signing

    from apps.loyalty.past_guest_offers import SALT

    booking, _ = offer(rig)
    proof = signing.loads(booking.loyalty_state[KEY]["proof"], salt=SALT)
    target = {"phone": PHONE, "customer": proof["customer"], "reward": proof["quote"]}
    assert retain_sent_offer(booking, target)
    TelegramWebsiteHandoff.objects.update(provider_message_id="322")
    assert not retain_sent_offer(booking, target)


def test_receipt_callback_applies_existing_verified_member_offer_after_commit(
    rig, django_capture_on_commit_callbacks
):
    from apps.loyalty import booking_invitations

    booking, _ = offer(rig, state="handoff_wait")
    guest = user("existing-group-recipient")
    VerifiedPhone.objects.create(user=guest, phone=PHONE)
    member = join(guest)
    assert not Ledger.objects.exists()
    request_id = next(iter(booking.loyalty_state["owner_join_invitation_requests"]))
    with django_capture_on_commit_callbacks(execute=True):
        booking_invitations._finish(
            booking.pk,
            "handed_off",
            expected="handoff_wait",
            request_id=request_id,
            provider_id="321",
        )
    assert balance(member) == Decimal("15.00")
    assert Ledger.objects.filter(reservation=booking).count() == 1
