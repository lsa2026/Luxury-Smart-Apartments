"""Offline manual repeat approvals, visible receipt times and unchanged automatic suppression."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import RequestFactory

from apps.accounts.models import VerifiedPhone
from apps.loyalty import invitation_registry as registry
from apps.loyalty.models import JoinInvitation, Ledger
from apps.loyalty.past_guest_offers import KEY, apply_for_user
from apps.loyalty.services import balance
from tests import test_loyalty_booking_invitations as manual
from tests.test_loyalty_stage8 import join, user

pytestmark = pytest.mark.django_db
rig = manual.rig


def request_send(rig, booking, channel="whatsapp", *, confirm=False):
    data = manual.payload(rig[0], booking, channel)
    if confirm:
        data["confirm_resend"] = "on"
    response = rig[0].post(manual.url(booking), data)
    assert response.status_code == 302
    return data


def first_send(rig, booking, *, uncertain=False):
    if uncertain:
        rig[2].send_image.side_effect = TimeoutError()
    request_send(rig, booking)
    manual.deliver_latest(booking)
    booking.refresh_from_db()


def test_existing_receipt_is_visible_with_riyadh_times_and_repeat_warning(rig):
    booking = manual.past_booking()
    first_send(rig, booking)
    before = deepcopy(booking.loyalty_state)
    page = rig[0].get(manual.url(booking))
    html = page.content.decode()
    assert "سجل دعوات هذا الحجز" in html and "بتوقيت الرياض" in html
    assert "2026-10-22 18:00:00" in html
    assert "توجد دعوات سابقة" in html and 'name="confirm_resend" required' in html
    receipt = page.context["loyalty_invitation"]["history"][0]
    assert receipt["status"] == "accepted" and receipt["updated_at"]
    assert all(row["confirmation"] for row in page.context["loyalty_invitation"]["channels"])
    booking.refresh_from_db()
    assert booking.loyalty_state == before and rig[2].send_image.call_count == 1


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("channel", ["whatsapp", "email"])
def test_confirmed_repeat_has_own_receipt_preserves_offer_and_awards_only_once(
    rig, mailoutbox, uncertain, channel
):
    booking = manual.past_booking()
    first_send(rig, booking, uncertain=uncertain)
    original_offer = deepcopy(booking.loyalty_state[KEY])
    original_receipts = deepcopy(booking.loyalty_state[manual.invitations.REQUESTS])
    original_shared = JoinInvitation.objects.values().get()
    rig[2].send_image.side_effect = None
    rig[2].send_image.return_value = {"id": "second-provider-message"}
    data = request_send(rig, booking, channel, confirm=True)
    manual.deliver_latest(booking, channel)
    manual.deliver_latest(booking, channel)  # duplicate task is not another provider POST
    booking.refresh_from_db()
    assert booking.loyalty_state[KEY] == original_offer
    for key, row in original_receipts.items():
        assert booking.loyalty_state[manual.invitations.REQUESTS][key] == row
    assert JoinInvitation.objects.values().get() == original_shared
    rows = rig[0].get(manual.url(booking)).context["loyalty_invitation"]["history"]
    assert len(rows) == 2 and sum(row["manual_resend"] for row in rows) == 1
    assert rig[2].send_image.call_count == (2 if channel == "whatsapp" else 1)
    assert len(mailoutbox) == (1 if channel == "email" else 0)
    if channel == "whatsapp":
        references = [call.kwargs["reference_id"] for call in rig[2].send_image.call_args_list]
        assert references[0] != references[1] and ":manual:" in references[1]
    rig[0].post(manual.url(booking), data)  # same approved press remains idempotent
    assert len(rig[0].get(manual.url(booking)).context["loyalty_invitation"]["history"]) == 2
    with registry.invitation_lock():
        decision, _ = registry.claim(
            key=registry.booking_key(booking),
            phone="+12025550123",
            email="",
            customer_id=None,
            language="ar",
            event_id="synthetic-automatic",
            source="external",
            origin="n8n",
            channel="whatsapp",
        )
    assert decision == "duplicate"
    guest = user("repeat-recipient")
    VerifiedPhone.objects.create(user=guest, phone="+12025550123")
    member = join(guest)
    apply_for_user(guest)
    assert balance(member) == Decimal("15.00")
    assert Ledger.objects.filter(reservation=booking).count() == 1
    assert "سجل دعوات هذا الحجز" in rig[0].get(manual.url(booking)).content.decode()


def test_repeat_requires_fresh_explicit_checkbox_and_never_replays_on_get(rig):
    booking = manual.past_booking()
    first_send(rig, booking)
    request_send(rig, booking)
    booking.refresh_from_db()
    assert len(booking.loyalty_state[manual.invitations.REQUESTS]) == 1
    assert rig[2].send_image.call_count == 1
    for _ in range(2):
        rig[0].get(manual.url(booking))
    assert rig[2].send_image.call_count == 1


def test_pending_request_cannot_be_overlapped_and_stale_history_requires_review(rig):
    booking = manual.past_booking()
    request_send(rig, booking)
    stale = request_send(rig, booking, confirm=True)
    booking.refresh_from_db()
    assert len(booking.loyalty_state[manual.invitations.REQUESTS]) == 1
    manual.deliver_latest(booking)
    rig[0].post(manual.url(booking), stale)
    booking.refresh_from_db()
    assert len(booking.loyalty_state[manual.invitations.REQUESTS]) == 1
    assert rig[2].send_image.call_count == 1


def test_legacy_receipt_without_registry_is_retained_and_can_be_manually_repeated(rig):
    booking = manual.past_booking()
    old = {"status": "accepted", "requested_at": "2026-10-21T09:00:00+00:00"}
    booking.loyalty_state[manual.invitations.STATE] = old
    booking.save()
    request_send(rig, booking, confirm=True)
    manual.deliver_latest(booking)
    booking.refresh_from_db()
    assert booking.loyalty_state[manual.invitations.STATE] == old
    assert rig[2].send_image.call_count == 1
    assert JoinInvitation.objects.get().state == "accepted"


def test_changed_reward_cannot_replace_a_sent_offer_during_repeat(rig):
    booking = manual.past_booking()
    first_send(rig, booking)
    original_offer = deepcopy(booking.loyalty_state[KEY])
    q = booking.booking_intent.quote
    q.components = [
        {"type": "baseRate", "total": "400.00", "included_in_total": True},
        {"type": "cleaningFee", "total": "100.00", "included_in_total": True},
    ]
    q.save()
    request_send(rig, booking, confirm=True)
    booking.refresh_from_db()
    assert booking.loyalty_state[KEY] == original_offer
    assert len(booking.loyalty_state[manual.invitations.REQUESTS]) == 1
    assert rig[2].send_image.call_count == 1


def test_guest_joining_before_repeat_worker_stops_repeat_without_losing_old_offer(rig):
    booking = manual.past_booking()
    first_send(rig, booking)
    original_offer = deepcopy(booking.loyalty_state[KEY])
    request_send(rig, booking, confirm=True)
    guest = user("join-before-repeat")
    VerifiedPhone.objects.create(user=guest, phone="+12025550123")
    member = join(guest)
    manual.deliver_latest(booking)
    booking.refresh_from_db()
    assert manual.whatsapp_row(booking)["status"] == "blocked"
    assert booking.loyalty_state[KEY] == original_offer
    assert balance(member) == Decimal("15.00")
    assert rig[2].send_image.call_count == 1


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_repeat_approvals_allow_only_one_new_send(rig):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL advisory locks and independent connections.")
    booking = manual.past_booking()
    first_send(rig, booking)
    proofs = [dict(manual.payload(rig[0], booking), confirm_resend="on") for _ in range(2)]
    barrier = Barrier(2)

    def run(data):
        close_old_connections()
        try:
            request = RequestFactory().post(manual.url(booking), data)
            request.user = rig[1]
            barrier.wait(timeout=10)
            try:
                return manual.invitations.queue_booking_invitation(request, booking.pk)
            except ValidationError:
                return "stale"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, proofs))
    assert results.count("stale") == 1
    manual.deliver_latest(booking)
    manual.deliver_latest(booking)
    booking.refresh_from_db()
    assert len(booking.loyalty_state[manual.invitations.REQUESTS]) == 2
    assert rig[2].send_image.call_count == 2
