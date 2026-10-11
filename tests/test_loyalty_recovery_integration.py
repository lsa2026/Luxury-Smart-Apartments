"""A read-only provider double resolves uncertain points creates; no live calls."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.loyalty.checkout import confirm_points_booking
from apps.loyalty.models import Ledger
from apps.loyalty.recovery import recover_points_booking
from apps.loyalty.services import hold
from apps.notifications.models import Notification
from apps.reservations.models import Reservation
from apps.reservations.services.hostaway_booking import prepare_local_reservation
from tests.test_hostaway_booking_phase5 import create_result
from tests.test_loyalty_checkout_integration import checkout
from tests.test_loyalty_checkout_integration import program as program

pytestmark = pytest.mark.django_db


def uncertain():
    guest, member, intent, _ = checkout()
    booking = prepare_local_reservation(intent)
    hold(guest, "TEST-RECOVERY-HOLD", booking.pk, str(intent.total_price))
    confirm_points_booking(
        guest,
        booking.pk,
        booking_service=SimpleNamespace(
            create_points_reservation=lambda _: SimpleNamespace(code="hostaway_create_uncertain")
        ),
    )
    guest.is_staff = guest.is_superuser = True
    guest.save()
    booking.refresh_from_db()
    snapshot = create_result(booking).snapshot
    assert (
        Notification.objects.filter(idempotency_key=f"loyalty-uncertain:{booking.pk}").count() == 1
    )
    return guest, member, booking, snapshot


def test_verified_existing_booking_settles_once_without_recreating_or_charging(settings):
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = False
    guest, member, booking, snapshot = uncertain()
    reads = []
    client = SimpleNamespace(get_reservation=lambda external: reads.append(external) or snapshot)
    args = {
        "external_id": snapshot.reservation_id,
        "reference": "TEST-OWNER-REVIEW",
        "guest_identity_confirmed": True,
        "client": client,
    }
    assert recover_points_booking(guest, booking.pk, **args)["status"] == "confirmed"
    assert recover_points_booking(guest, booking.pk, **args)["status"] == "already_confirmed"
    booking.refresh_from_db()
    assert booking.normalized_status == Reservation.Status.CONFIRMED
    assert (
        booking.booking_intent.status == "completed"
        and booking.payment_status == "paid_with_points"
    )
    assert Ledger.objects.filter(member=member, event_type="redeem").count() == 1
    assert not booking.booking_intent.payment_attempts.exists()
    assert reads == [snapshot.reservation_id, snapshot.reservation_id]


def test_wrong_property_or_unconfirmed_external_booking_does_not_spend_points(settings):
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = False
    guest, member, booking, snapshot = uncertain()
    for wrong in [replace(snapshot, listing_map_id=99999), replace(snapshot, status="cancelled")]:
        with pytest.raises(ValidationError):
            recover_points_booking(
                guest,
                booking.pk,
                external_id=wrong.reservation_id,
                reference="TEST",
                guest_identity_confirmed=True,
                client=SimpleNamespace(get_reservation=lambda _, value=wrong: value),
            )
    assert not Ledger.objects.filter(member=member, event_type="redeem").exists()


def test_identity_confirmation_and_owner_gate_checked_before_provider(settings):
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = False
    guest, _, booking, snapshot = uncertain()
    calls = []
    client = SimpleNamespace(get_reservation=lambda _: calls.append(timezone.now()))
    with pytest.raises(ValidationError):
        recover_points_booking(
            guest,
            booking.pk,
            external_id=snapshot.reservation_id,
            reference="TEST",
            guest_identity_confirmed=False,
            client=client,
        )
    guest.is_staff = guest.is_superuser = False
    guest.save()
    with pytest.raises(PermissionDenied):
        recover_points_booking(
            guest,
            booking.pk,
            external_id=snapshot.reservation_id,
            reference="TEST",
            guest_identity_confirmed=True,
            client=client,
        )
    assert not calls
