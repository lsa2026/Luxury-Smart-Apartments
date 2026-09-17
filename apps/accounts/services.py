"""Attaching a guest booking to a customer account.

A booking made without an account keeps ``BookingIntent.customer`` empty, so it
would never reach the dashboard. Linking it later is safe only when the visitor
has already proven they hold the booking.

Matching on the email address alone would not be proof: nobody has verified that
address, so anyone registering with a guest's email would be handed their name,
phone, billing address and stay. The session grant issued after a verified
payment, or after the reference-and-email challenge, is the proof relied on here.
"""

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.http import HttpRequest

from apps.reservations.models import BookingIntent, Reservation
from apps.reservations.security import session_can_manage


def claim_reservation(
    request: HttpRequest,
    user: AbstractBaseUser,
    public_reference: str,
) -> Reservation | None:
    """Attach one session-proven reservation to ``user``.

    Returns the reservation when it was newly attached, and ``None`` when the
    session cannot prove access, the booking is already owned, or the reference
    matches nothing. A reference the caller cannot prove is treated exactly like
    one that does not exist, so the endpoint reveals nothing either way.
    """
    reference = (public_reference or "").strip()
    if not reference or not session_can_manage(request, reference):
        return None

    with transaction.atomic():
        reservation = (
            Reservation.objects.select_related("booking_intent")
            .select_for_update(of=("self",))
            .filter(public_reference=reference)
            .first()
        )
        if reservation is None:
            return None
        intent = reservation.booking_intent
        # An intent already owned is left alone: re-assigning it would silently
        # move a booking between accounts.
        if intent is None or intent.customer_id is not None:
            return None
        intent.customer = user
        intent.save(update_fields=["customer"])
    return reservation


def claimable_reference(request: HttpRequest, public_reference: str) -> str:
    """Return the reference only when this session may still claim it."""
    reference = (public_reference or "").strip()
    if not reference or not session_can_manage(request, reference):
        return ""
    unclaimed = Reservation.objects.filter(
        public_reference=reference,
        booking_intent__customer__isnull=True,
    ).exists()
    return reference if unclaimed else ""


def claim_reservations_for_verified_email(user: AbstractBaseUser) -> int:
    """Attach direct bookings only after the guest proves their inbox."""
    email = (getattr(user, "email", "") or "").strip().casefold()
    if not email or not getattr(user, "pk", None):
        return 0
    with transaction.atomic():
        return BookingIntent.objects.filter(
            customer__isnull=True,
            guest_email__iexact=email,
        ).update(customer=user)
