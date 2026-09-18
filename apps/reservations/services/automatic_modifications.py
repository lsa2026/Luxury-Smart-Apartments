"""Automatic happy-path orchestration for direct Hostaway modifications."""

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.payments.currency import PAYMENT_CURRENCY
from apps.payments.models import PaymentAttempt

from ..models import BookingModificationRequest, RefundObligation, Reservation
from .hostaway_modifications import HostawayModificationService, ModificationExecution
from .owner_settlement import owner_change_blocker, settlement_for
from .refunds import (
    RefundComputation,
    cancellation_refund,
    record_obligation,
    successful_original_payment_amount,
)

if TYPE_CHECKING:
    from apps.payments.hyperpay.refunds import HyperPayRefundService


@dataclass(frozen=True, slots=True)
class AutomaticModificationOutcome:
    code: str
    request: BookingModificationRequest
    execution: ModificationExecution | None = None
    refund: RefundObligation | None = None
    refund_code: str | None = None


def execute_automatic_modification(
    modification: BookingModificationRequest,
    *,
    service: HostawayModificationService | None = None,
    refund_service: "HyperPayRefundService | None" = None,
    approved_refund_amount: Decimal | None = None,
    refund_decision_note: str = "",
    owner_override: bool = False,
) -> AutomaticModificationOutcome:
    """Approve and execute a safe modification without an administrative hop.

    Positive differences require a matching verified payment. A price decrease
    is refunded to the original payment method only after Hostaway confirms the
    amended reservation.
    """

    automatic_cancellation = (
        modification.request_type == BookingModificationRequest.RequestType.CANCEL_RESERVATION
        and settings.BOOKING_AUTOMATIC_CANCELLATION_ENABLED
    )
    if not (
        settings.BOOKING_AUTOMATIC_MODIFICATION_APPROVAL or automatic_cancellation or owner_override
    ):
        # A successful difference payment must never leave the request claiming
        # that payment is still due when automatic execution is intentionally off.
        if modification.status == BookingModificationRequest.Status.AWAITING_PAYMENT:
            paid = PaymentAttempt.objects.filter(
                modification_request=modification,
                status=PaymentAttempt.Status.SUCCEEDED,
                amount=modification.payment_amount_sar,
                currency=PAYMENT_CURRENCY,
                verified_at__isnull=False,
            ).exists()
            if paid:
                BookingModificationRequest.objects.filter(pk=modification.pk).update(
                    status=BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL,
                    updated_at=timezone.now(),
                )
                modification.refresh_from_db()
        return AutomaticModificationOutcome("automatic_approval_disabled", modification)

    with transaction.atomic():
        Reservation.objects.select_for_update().get(pk=modification.reservation_id)
        locked = (
            BookingModificationRequest.objects.select_for_update()
            .select_related("reservation")
            .get(pk=modification.pk)
        )
        if owner_override:
            blocker = owner_change_blocker(locked, completed=True)
            if blocker:
                return AutomaticModificationOutcome(blocker, locked)
        if locked.status == BookingModificationRequest.Status.COMPLETED:
            return AutomaticModificationOutcome("already_completed", locked)
        if owner_override:
            settlement = settlement_for(locked.reservation, locked.new_total)
            preview = locked.quote_snapshot.get("owner_settlement", {})
            if preview and Decimal(preview["paid"]) != settlement.paid:
                return AutomaticModificationOutcome("payment_changed_reprice_required", locked)
            if (
                approved_refund_amount is not None
                and not Decimal("0") <= approved_refund_amount <= settlement.refund
            ):
                return AutomaticModificationOutcome("invalid_refund_amount", locked)
        if locked.request_type == BookingModificationRequest.RequestType.CANCEL_RESERVATION:
            if not settings.BOOKING_AUTOMATIC_CANCELLATION_ENABLED:
                return AutomaticModificationOutcome("automatic_cancellation_disabled", locked)
            # Recheck under the reservation lock, before making any external change.
            ceiling = cancellation_refund(locked.reservation).amount
            if (
                approved_refund_amount is not None
                and not Decimal("0") <= approved_refund_amount <= ceiling
            ):
                return AutomaticModificationOutcome("payment_changed_reprice_required", locked)
        elif locked.price_difference > 0 and not owner_override:
            paid = PaymentAttempt.objects.filter(
                modification_request=locked,
                status=PaymentAttempt.Status.SUCCEEDED,
                amount=locked.payment_amount_sar,
                currency=PAYMENT_CURRENCY,
                verified_at__isnull=False,
            ).first()
            if paid is None:
                return AutomaticModificationOutcome("successful_payment_required", locked)
            if paid.provider == "hyperpay" and settings.HYPERPAY_ENVIRONMENT == "test":
                return AutomaticModificationOutcome("test_payment_live_write_blocked", locked)

        allowed = {
            BookingModificationRequest.Status.AWAITING_PAYMENT,
            BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL,
            BookingModificationRequest.Status.READY_FOR_HOSTAWAY,
        }
        if locked.status not in allowed:
            return AutomaticModificationOutcome("modification_not_eligible", locked)
        if (
            locked.status != BookingModificationRequest.Status.READY_FOR_HOSTAWAY
            or locked.approved_at is None
        ):
            now = timezone.now()
            locked.status = BookingModificationRequest.Status.READY_FOR_HOSTAWAY
            locked.approved_at = now
            locked.save(update_fields=["status", "approved_at", "updated_at"])

    if _is_orphaned_paid_cancellation(locked):
        return _refund_orphaned_paid_reservation(
            locked,
            refund_service=refund_service,
            approved_refund_amount=approved_refund_amount,
            refund_decision_note=refund_decision_note,
        )

    if service is not None:
        execution = service.execute(locked)
    else:
        with HostawayModificationService() as owned_service:
            execution = owned_service.execute(locked)
    refund = None
    refund_code = None
    if execution.code == "completed":
        refund = _record_refund_if_owed(
            execution.request,
            approved_refund_amount=approved_refund_amount,
            refund_decision_note=refund_decision_note,
        )
        if refund is not None and settings.BOOKING_AUTOMATIC_REFUND_ENABLED:
            refund_code = _submit_automatic_refund(refund, service=refund_service)
    return AutomaticModificationOutcome(
        execution.code,
        execution.request,
        execution,
        refund=refund,
        refund_code=refund_code,
    )


def _is_orphaned_paid_cancellation(modification: BookingModificationRequest) -> bool:
    """Whether payment succeeded but no Hostaway booking was ever created.

    This deliberately has no Hostaway cancellation branch: a missing external
    identifier means there is nothing to cancel there.  The only safe remedy is
    returning the card payment and closing the local record after the gateway
    accepts that return.
    """

    reservation = modification.reservation
    return (
        modification.request_type == BookingModificationRequest.RequestType.CANCEL_RESERVATION
        and reservation.source_type == Reservation.SourceType.DIRECT_WEBSITE
        and reservation.normalized_status == Reservation.Status.READY_FOR_HOSTAWAY
        and reservation.hostaway_reservation_id is None
    )


def _refund_orphaned_paid_reservation(
    modification: BookingModificationRequest,
    *,
    refund_service: "HyperPayRefundService | None",
    approved_refund_amount: Decimal | None,
    refund_decision_note: str,
) -> AutomaticModificationOutcome:
    """Refund an orphaned local charge without making a fictional Hostaway call."""

    if not settings.BOOKING_AUTOMATIC_REFUND_ENABLED:
        return AutomaticModificationOutcome("automatic_refund_disabled", modification)

    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=modification.reservation_id)
        locked = (
            BookingModificationRequest.objects.select_for_update()
            .select_related("reservation")
            .get(pk=modification.pk)
        )
        if locked.status == BookingModificationRequest.Status.COMPLETED:
            return AutomaticModificationOutcome("already_completed", locked)
        if not _is_orphaned_paid_cancellation(locked):
            return AutomaticModificationOutcome("orphaned_reservation_changed", locked)

        computation = cancellation_refund(reservation)
        if computation.amount <= Decimal("0"):
            return AutomaticModificationOutcome("original_payment_not_found", locked)
        amount = (
            computation.amount
            if approved_refund_amount is None
            else Decimal(approved_refund_amount)
        )
        if amount != computation.amount:
            # An orphaned charge never created a stay, so it must be returned in
            # full; partial/zero cancellation choices would leave a guest charged
            # for a booking that does not exist in the channel manager.
            return AutomaticModificationOutcome("orphaned_refund_must_be_full", locked)

        refund = (
            RefundObligation.objects.select_for_update()
            .filter(
                modification_request=locked,
                reason=RefundObligation.Reason.CANCELLATION,
            )
            .order_by("-created_at")
            .first()
        )
        if refund is None:
            detail = dict(computation.detail)
            detail.update(
                {
                    "source": "orphaned_local_paid_reservation",
                    "hostaway_reservation_id": None,
                    "operator_refund_decision": {
                        "calculated_amount": format(computation.amount, "f"),
                        "approved_amount": format(amount, "f"),
                        "note": refund_decision_note.strip()[:500],
                    },
                }
            )
            refund = record_obligation(
                reservation,
                reason=RefundObligation.Reason.CANCELLATION,
                computation=RefundComputation(
                    amount=amount, currency=computation.currency, detail=detail
                ),
                modification=locked,
            )
        if refund is None:
            return AutomaticModificationOutcome("original_payment_not_found", locked)
        if refund.status == RefundObligation.Status.TRANSFERRED:
            _complete_orphaned_local_cancellation(locked.pk)
            locked.refresh_from_db()
            return AutomaticModificationOutcome(
                "orphaned_refunded", locked, refund=refund, refund_code="refund.hyperpay_completed"
            )
        if refund.status == RefundObligation.Status.PROCESSING:
            return AutomaticModificationOutcome("orphaned_refund_pending", locked, refund=refund)

    refund_code = _submit_automatic_refund(refund, service=refund_service)
    if refund_code in {"refund.hyperpay_completed", "refund.hyperpay_submitted"}:
        _complete_orphaned_local_cancellation(locked.pk)
        locked.refresh_from_db()
        return AutomaticModificationOutcome(
            "orphaned_refunded", locked, refund=refund, refund_code=refund_code
        )
    return AutomaticModificationOutcome(
        "orphaned_refund_not_accepted", locked, refund=refund, refund_code=refund_code
    )


def _complete_orphaned_local_cancellation(modification_id: object) -> None:
    """Close only the local record after HyperPay accepts the full refund."""

    with transaction.atomic():
        locked = (
            BookingModificationRequest.objects.select_for_update()
            .select_related("reservation")
            .get(pk=modification_id)
        )
        reservation = Reservation.objects.select_for_update().get(pk=locked.reservation_id)
        if locked.status == BookingModificationRequest.Status.COMPLETED:
            return
        now = timezone.now()
        reservation.normalized_status = Reservation.Status.CANCELLED
        reservation.cancelled_at = now
        reservation.confirmed_at = None
        reservation.hostaway_status = "not_created"
        reservation.last_synced_at = now
        reservation.full_clean()
        reservation.save()
        locked.status = BookingModificationRequest.Status.COMPLETED
        locked.completed_at = now
        locked.save(update_fields=["status", "completed_at", "updated_at"])
        from apps.notifications.services.events import handle_modification_completed

        transaction.on_commit(lambda: handle_modification_completed(locked.pk))


def _record_refund_if_owed(
    modification: BookingModificationRequest,
    *,
    approved_refund_amount: Decimal | None = None,
    refund_decision_note: str = "",
) -> RefundObligation | None:
    """Write down what the guest is owed, only after Hostaway accepted the change.

    Recording first would risk a debt for a change that never happened; recording
    only on success means the money owed always matches a real booking state.
    """
    reservation = modification.reservation
    if modification.request_type == BookingModificationRequest.RequestType.CANCEL_RESERVATION:
        computation = cancellation_refund(reservation)
        reason = RefundObligation.Reason.CANCELLATION
    elif modification.quote_snapshot.get("owner_settlement") is not None:
        settlement = settlement_for(reservation, modification.new_total)
        if settlement.refund <= 0:
            return None
        computation = RefundComputation(
            amount=settlement.refund,
            currency=modification.currency,
            detail={
                "source": "owner_final_total_vs_net_paid",
                "net_paid": format(settlement.paid, "f"),
                "new_total": format(modification.new_total, "f"),
            },
        )
        reason = RefundObligation.Reason.MODIFICATION_DECREASE
    elif modification.price_difference < 0:
        original_payment = successful_original_payment_amount(reservation)
        if original_payment is None:
            return None
        refundable_difference = min(abs(modification.price_difference), original_payment)
        computation = RefundComputation(
            amount=refundable_difference,
            currency=modification.currency,
            detail={
                "old_total": format(modification.old_total, "f"),
                "new_total": format(modification.new_total or Decimal("0"), "f"),
                "original_payment_ceiling": format(original_payment, "f"),
                "source": "modification_price_difference",
            },
        )
        reason = RefundObligation.Reason.MODIFICATION_DECREASE
    else:
        return None
    if approved_refund_amount is not None:
        requested_amount = Decimal(approved_refund_amount)
        if requested_amount < Decimal("0") or requested_amount > computation.amount:
            # The caller validates the owner's form. This guard keeps a direct
            # service invocation from refunding more than the verified amount.
            return None
        detail = dict(computation.detail)
        detail["operator_refund_decision"] = {
            "calculated_amount": format(computation.amount, "f"),
            "approved_amount": format(requested_amount, "f"),
            "note": refund_decision_note.strip()[:500],
        }
        computation = RefundComputation(
            amount=requested_amount,
            currency=computation.currency,
            detail=detail,
        )
    return record_obligation(
        reservation,
        reason=reason,
        computation=computation,
        modification=modification,
    )


def _submit_automatic_refund(
    refund: RefundObligation,
    *,
    service: "HyperPayRefundService | None" = None,
) -> str:
    """Return money only after Hostaway has confirmed the cancellation.

    A provider error never reopens or reverses a confirmed cancellation.  The
    obligation remains visible to the operations team for a safe retry.
    """
    from apps.payments.hyperpay.exceptions import HyperPayRefundError
    from apps.payments.hyperpay.refunds import HyperPayRefundService

    try:
        outcome = (service or HyperPayRefundService()).submit(refund, operator=None)
    except HyperPayRefundError as exc:
        return exc.code
    return outcome.audit_action
