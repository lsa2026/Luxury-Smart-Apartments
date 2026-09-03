"""Automatic happy-path orchestration for direct Hostaway modifications."""

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.payments.models import PaymentAttempt

from ..models import BookingModificationRequest
from .hostaway_modifications import HostawayModificationService, ModificationExecution


@dataclass(frozen=True, slots=True)
class AutomaticModificationOutcome:
    code: str
    request: BookingModificationRequest
    execution: ModificationExecution | None = None


def execute_automatic_modification(
    modification: BookingModificationRequest,
    *,
    service: HostawayModificationService | None = None,
) -> AutomaticModificationOutcome:
    """Approve and execute a safe modification without an administrative hop.

    Positive differences require a matching verified payment. Price decreases
    remain an explicit exception until an automated refund workflow exists.
    """

    if not settings.BOOKING_AUTOMATIC_MODIFICATION_APPROVAL:
        # A successful difference payment must never leave the request claiming
        # that payment is still due when automatic execution is intentionally off.
        if modification.status == BookingModificationRequest.Status.AWAITING_PAYMENT:
            paid = PaymentAttempt.objects.filter(
                modification_request=modification,
                status=PaymentAttempt.Status.SUCCEEDED,
                amount=modification.price_difference,
                currency=modification.currency,
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
        locked = (
            BookingModificationRequest.objects.select_for_update()
            .select_related("reservation")
            .get(pk=modification.pk)
        )
        if locked.status == BookingModificationRequest.Status.COMPLETED:
            return AutomaticModificationOutcome("already_completed", locked)
        if locked.request_type == BookingModificationRequest.RequestType.CANCEL_RESERVATION:
            if not settings.BOOKING_AUTOMATIC_CANCELLATION_ENABLED:
                return AutomaticModificationOutcome("automatic_cancellation_disabled", locked)
        elif locked.price_difference < 0:
            return AutomaticModificationOutcome("automatic_refund_required", locked)
        elif locked.price_difference > 0:
            paid = PaymentAttempt.objects.filter(
                modification_request=locked,
                status=PaymentAttempt.Status.SUCCEEDED,
                amount=locked.price_difference,
                currency=locked.currency,
                verified_at__isnull=False,
            ).first()
            if paid is None:
                return AutomaticModificationOutcome("successful_payment_required", locked)
            if (
                paid.provider == "hyperpay"
                and settings.HYPERPAY_ENVIRONMENT == "test"
            ):
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

    if service is not None:
        execution = service.execute(locked)
    else:
        with HostawayModificationService() as owned_service:
            execution = owned_service.execute(locked)
    return AutomaticModificationOutcome(execution.code, execution.request, execution)
