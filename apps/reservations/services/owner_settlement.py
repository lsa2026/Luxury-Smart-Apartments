"""Settlement for owner-priced changes, independent of the old quoted total."""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from apps.payments.models import PaymentAttempt

from ..models import BookingModificationRequest, RefundObligation, Reservation


@dataclass(frozen=True)
class OwnerSettlement:
    paid: Decimal
    due: Decimal
    refund: Decimal


def settlement_blocker(reservation: Reservation) -> str:
    """Do not mistake an unmatched-currency collection for an unpaid booking."""
    if (
        reservation.booking_intent_id
        and PaymentAttempt.objects.filter(
            booking_intent_id=reservation.booking_intent_id,
            status__in=[PaymentAttempt.Status.SUCCEEDED, PaymentAttempt.Status.PARTIALLY_REFUNDED],
            verified_at__isnull=False,
        )
        .exclude(currency=reservation.currency)
        .exists()
    ):
        return "payment_currency_reconciliation_required"
    if reservation.refund_obligations.filter(
        status__in=[RefundObligation.Status.DUE, RefundObligation.Status.PROCESSING],
        amount__gt=0,
    ).exists():
        return "previous_refund_requires_reconciliation"
    return ""


def settlement_for(reservation: Reservation, final_total: Decimal) -> OwnerSettlement:
    """Count verified collections, less refunded or reserved-for-refund money."""
    payments = PaymentAttempt.objects.none()
    if reservation.booking_intent_id:
        payments = PaymentAttempt.objects.filter(
            booking_intent_id=reservation.booking_intent_id,
            status__in=[
                PaymentAttempt.Status.SUCCEEDED,
                PaymentAttempt.Status.PARTIALLY_REFUNDED,
                PaymentAttempt.Status.REFUNDED,
            ],
            verified_at__isnull=False,
            currency=reservation.currency,
        )
    collected = payments.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    returned = reservation.refund_obligations.filter(
        status__in=[RefundObligation.Status.PROCESSING, RefundObligation.Status.TRANSFERRED],
        currency=reservation.currency,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    paid = max(collected - returned, Decimal("0"))
    return OwnerSettlement(
        paid, max(final_total - paid, Decimal("0")), max(paid - final_total, Decimal("0"))
    )


def owner_change_blocker(modification: BookingModificationRequest, *, completed=False) -> str:
    if modification.request_type == BookingModificationRequest.RequestType.CANCEL_RESERVATION:
        return "not_price_editable"
    if modification.quote_snapshot.get("owner_final_total") is None:
        return "final_price_required"
    latest = (
        modification.reservation.modification_requests.exclude(
            request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION
        )
        .order_by("-requested_at", "-pk")
        .values_list("pk", flat=True)
        .first()
    )
    if latest != modification.pk:
        return "modification_superseded"
    reservation = modification.reservation
    blocker = settlement_blocker(reservation)
    if blocker:
        return blocker
    if reservation.normalized_status not in {
        Reservation.Status.CONFIRMED,
        Reservation.Status.MODIFIED,
    }:
        return "reservation_not_confirmed"
    if completed and modification.status == BookingModificationRequest.Status.COMPLETED:
        if (
            reservation.check_in != modification.new_check_in
            or reservation.check_out != modification.new_check_out
            or reservation.total_price != modification.new_total
        ):
            return "reservation_changed"
        return ""
    if modification.is_expired:
        return "expired"
    if modification.status not in {
        BookingModificationRequest.Status.READY_FOR_HOSTAWAY,
        BookingModificationRequest.Status.AWAITING_PAYMENT,
    }:
        return "modification_not_eligible"
    reservation = modification.reservation
    if reservation.normalized_status not in {
        Reservation.Status.CONFIRMED,
        Reservation.Status.MODIFIED,
    }:
        return "reservation_not_confirmed"
    if (
        reservation.check_in != modification.old_check_in
        or reservation.check_out != modification.old_check_out
        or reservation.guests != modification.old_guests
        or reservation.total_price != modification.old_total
        or reservation.currency != modification.currency
    ):
        return "reservation_changed"
    return ""
