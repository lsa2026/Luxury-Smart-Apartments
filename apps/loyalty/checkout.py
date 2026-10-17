"""Cash/points boundary: preserve the full quote, reduce only the cash collection."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection

from apps.payments.models import PaymentAttempt
from apps.reservations.models import Reservation

from .launch import booking_allowed


def cash_due(intent, *, freeze=False):
    from .services import enabled, member_for

    gross = intent.payment_amount_sar
    if not enabled():
        # A rollback must not turn an existing points allocation into a larger
        # card charge. Pause checkout without changing its hold, ledger, or
        # provider receipt. Cash-only bookings keep their normal payment path.
        state = (
            Reservation.objects.filter(booking_intent=intent)
            .values_list("loyalty_state", flat=True)
            .first()
        ) or {}
        if any(
            state.get(key)
            for key in (
                "hold",
                "redemption",
                "checkout_frozen",
                "points_confirmation_started",
            )
        ):
            raise ValidationError("برنامج النقاط متوقف؛ يلزم حسم تسوية هذا الحجز قبل الدفع.")
        return gross
    # Checkout holds the intent lock; financial commands do not lock intents.
    # The same financial lock serializes changes to the hold and its freeze.
    if freeze and connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(190009, 8)")
    booking = Reservation.objects.filter(booking_intent=intent).first()
    if not booking:
        return gross
    allocation = booking.loyalty_state.get("hold") or booking.loyalty_state.get("redemption")
    if not allocation:
        return gross
    if not booking_allowed(booking):
        raise ValidationError("الحجز خارج نطاق التجربة الخاصة؛ لا يُغيّر المبلغ المخصص للدفع.")
    from .valuation import intent_total_sar

    if not intent.customer_id or gross != intent_total_sar(intent):
        raise ValidationError("يلزم تفصيل دفع نقاط موثق بالريال السعودي.")
    member = member_for(intent.customer)
    if allocation.get("member", member.pk) != member.pk:
        raise ValidationError("النقاط المحجوزة تخص حسابًا مختلفًا.")
    due = gross - Decimal(allocation["value"])
    if due < 0:
        raise ValidationError("النقاط تتجاوز قيمة الحجز.")
    if freeze:
        booking.loyalty_state["checkout_frozen"] = {"cash": str(due), "points": allocation["value"]}
        booking.save(update_fields=["loyalty_state", "updated_at"])
    return due


def release_allowed(booking):
    if booking.loyalty_state.get("points_confirmation_started"):
        raise ValidationError("لا تحرر النقاط قبل حسم نتيجة تأكيد الحجز.")
    if (
        booking.loyalty_state.get("checkout_frozen")
        and PaymentAttempt.objects.filter(
            booking_intent=booking.booking_intent,
            status__in=[
                PaymentAttempt.Status.CREATED,
                PaymentAttempt.Status.PENDING,
                PaymentAttempt.Status.UNKNOWN,
                PaymentAttempt.Status.REVIEW,
                PaymentAttempt.Status.SUCCEEDED,
                PaymentAttempt.Status.PARTIALLY_REFUNDED,
                PaymentAttempt.Status.REFUNDED,
            ],
        ).exists()
    ):
        raise ValidationError("لا تحرر النقاط بينما عملية الدفع قائمة أو تم قبضها.")


def settle_booking_points(reservation_id):
    from .services import enabled, redeem

    if not enabled():
        return
    booking = Reservation.objects.select_related("booking_intent__customer").get(pk=reservation_id)
    if not booking_allowed(booking):
        return
    if booking.normalized_status in Reservation.ACTIVE_STATUSES and booking.loyalty_state.get(
        "hold"
    ):
        return redeem(booking.booking_intent.customer, f"confirm:{booking.pk}", booking.pk)


def release_expired_points(intent_id):
    """Release only a terminal, definitely unpaid local hold; no provider query."""
    from apps.reservations.models import BookingIntent

    from .services import enabled, posting_transaction

    if not enabled():
        return
    with posting_transaction():
        booking = (
            Reservation.objects.select_for_update().filter(booking_intent_id=intent_id).first()
        )
        if not booking or not booking.loyalty_state.get("hold"):
            return
        if not booking_allowed(booking):
            return
        if booking.normalized_status in Reservation.ACTIVE_STATUSES:
            return
        if booking.booking_intent.status != BookingIntent.Status.EXPIRED:
            return
        try:
            release_allowed(booking)
        except ValidationError:
            return  # Uncertain/paid outcomes keep their allocation for review.
        booking.loyalty_state.pop("hold", None)
        booking.loyalty_state.pop("checkout_frozen", None)
        booking.save(update_fields=["loyalty_state", "updated_at"])


def restore_cancelled_points(reservation_id):
    from .services import enabled, restore_points

    if not enabled():
        return
    booking = Reservation.objects.get(pk=reservation_id)
    if not booking_allowed(booking):
        return
    redeemed = booking.loyalty_state.get("redemption")
    if not redeemed or booking.normalized_status != Reservation.Status.CANCELLED:
        return
    from .accrual import effective_arrival

    if effective_arrival(booking):
        # Actual departure + priced-night snapshot must be supplied by the
        # trusted stay-ending workflow. Never assume cancellation == departure.
        departure = booking.loyalty_state.get("departure_settlement")
        if not departure:
            return {"status": "departure_refund_evidence_required"}
        return restore_points(booking.pk, departure["points_refund"], departure["reference"])
    return restore_points(booking.pk, redeemed["value"], f"cancel:{booking.pk}")


def confirm_points_booking(user, reservation_id, *, booking_service):
    """Full-points booking: normal inventory service, never a fictional card receipt.

    Caller supplies the existing booking service (stubbed in isolated tests).
    The dedicated service retains normal price and inventory safeguards.
    """
    from .services import _booking, posting_transaction, redeem

    with posting_transaction():
        booking = _booking(user, reservation_id)
        if booking.loyalty_state.get("redemption"):
            return {"status": "already_confirmed", "reservation": str(booking.pk)}
        if booking.loyalty_state.get("points_confirmation_started"):
            return {"status": "confirmation_in_progress", "reservation": str(booking.pk)}
        if (
            booking.normalized_status != Reservation.Status.AWAITING_PAYMENT
            or not booking.loyalty_state.get("hold")
            or cash_due(booking.booking_intent, freeze=True) != 0
        ):
            raise ValidationError("يلزم حجز جاهز مغطى بالنقاط بالكامل.")
        if (
            PaymentAttempt.objects.filter(booking_intent=booking.booking_intent)
            .exclude(
                status__in=[
                    PaymentAttempt.Status.FAILED,
                    PaymentAttempt.Status.CANCELLED,
                    PaymentAttempt.Status.EXPIRED,
                ],
            )
            .exists()
        ):
            raise ValidationError("هذا الحجز مرتبط بعملية دفع قائمة.")
        booking.refresh_from_db()
        booking.loyalty_state["points_confirmation_started"] = True
        booking.save(update_fields=["loyalty_state", "updated_at"])
    # Inventory is still revalidated by the existing service. No financial
    # database lock is held across a channel-manager request.
    outcome = booking_service.create_points_reservation(booking)
    if outcome.code in {"confirmed", "already_confirmed"}:
        redeem(user, f"points-confirm:{booking.pk}", booking.pk)
        Reservation.objects.filter(pk=booking.pk).update(payment_status="paid_with_points")
        from apps.notifications.services.events import handle_reservation_confirmed
        from apps.reservations.models import BookingIntent

        BookingIntent.objects.filter(pk=booking.booking_intent_id).update(
            status=BookingIntent.Status.COMPLETED
        )
        handle_reservation_confirmed(booking.pk)
    else:
        # Retain the hold for uncertain inventory outcomes; do not pretend the
        # provider failed, or re-create an uncertain reservation automatically.
        from .site_adapter import _safe_reconcile

        _safe_reconcile(booking.pk)
        if outcome.code in {
            "availability_lost",
            "revalidated_price_changed",
            "hostaway_create_rejected",
        }:
            # A verified pre-send failure/rejection is not an uncertain create.
            # Keep history, but do not strand spendable points on a failed stay.
            with posting_transaction():
                booking.refresh_from_db()
                booking.loyalty_state.pop("hold", None)
                booking.loyalty_state.pop("checkout_frozen", None)
                booking.loyalty_state.pop("points_confirmation_started", None)
                booking.save(update_fields=["loyalty_state", "updated_at"])
        else:
            from .recovery import alert_uncertain

            alert_uncertain(booking)
    return {"status": outcome.code, "reservation": str(booking.pk)}
