"""Future-only Hostaway receipt bookkeeping. Never collects or refunds money."""

import logging
from decimal import Decimal, InvalidOperation

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.integrations.monitoring import alert_operations
from apps.integrations.tasks import distributed_task_lock
from apps.payments.models import HostawayFinancialEntry, PaymentAttempt
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)


def queue_payment_receipt(attempt_id: object) -> None:
    # Bookkeeping is secondary: provider/accounting outages must not turn a
    # verified guest payment into a website error or invite a second payment.
    try:
        _queue_verified_receipt(attempt_id)
    except Exception:
        logger.exception("Receipt queue needs operations review")
        alert_operations(f"receipt-queue:{attempt_id}")


def _queue_verified_receipt(attempt_id: object) -> None:
    cutoff = parse_datetime(settings.HOSTAWAY_FINANCIAL_RECEIPTS_START_AT)
    if (
        not settings.HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED
        or settings.HYPERPAY_ENVIRONMENT != "production"
        or cutoff is None
        or timezone.is_naive(cutoff)
    ):
        return
    attempt = PaymentAttempt.objects.filter(
        pk=attempt_id,
        provider="hyperpay",
        verified_at__gte=cutoff,
        status__in=["succeeded", "partially_refunded", "refunded"],
    ).first()
    if not attempt or not attempt.provider_payment_id or attempt.amount <= 0:
        return
    reservation = Reservation.objects.filter(
        booking_intent_id=attempt.booking_intent_id,
        hostaway_reservation_id__isnull=False,
    ).first()
    if not reservation:
        return
    entry, _ = HostawayFinancialEntry.objects.get_or_create(payment_attempt=attempt)
    if entry.status == "pending":
        try:
            transaction.on_commit(lambda: _dispatch_receipt(entry.pk), robust=True)
        except Exception:
            logger.exception("Receipt dispatch failed")
            alert_operations(f"receipt-dispatch:{entry.pk}")


@shared_task(name="apps.payments.hostaway_ledger.record_payment_receipt_task")
def record_payment_receipt_task(entry_id: str) -> dict[str, str]:
    cutoff = parse_datetime(settings.HOSTAWAY_FINANCIAL_RECEIPTS_START_AT)
    if (
        not settings.HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED
        or settings.HYPERPAY_ENVIRONMENT != "production"
        or cutoff is None
        or timezone.is_naive(cutoff)
    ):
        return {"status": "disabled"}
    with distributed_task_lock(f"hostaway-receipt:{entry_id}") as acquired:
        if not acquired:
            return {"status": "already_running"}
        entry = HostawayFinancialEntry.objects.select_related("payment_attempt").get(pk=entry_id)
        if entry.status == "succeeded":
            return {"status": "already_recorded"}
        attempt = entry.payment_attempt
        if not attempt.verified_at or attempt.verified_at < cutoff:
            return {"status": "historical_payment_skipped"}
        reservation = Reservation.objects.get(booking_intent_id=attempt.booking_intent_id)
        marker = f"LSA HyperPay receipt {attempt.pk}"
        try:
            with HostawayClient() as client:
                snapshot = client.get_reservation(reservation.hostaway_reservation_id)
                if (
                    not attempt.verified_at
                    or not attempt.provider_payment_id
                    or attempt.status not in {"succeeded", "partially_refunded", "refunded"}
                    or snapshot.reservation_id != reservation.hostaway_reservation_id
                    or snapshot.listing_map_id != reservation.hostaway_listing_map_id
                    or snapshot.currency != attempt.currency
                    or reservation.currency != attempt.currency
                ):
                    raise ValueError("receipt_requires_accounting_review")
                charges = client.get_guest_charges(reservation.hostaway_reservation_id)
                matches = [charge for charge in charges if charge.get("title") == marker]
                if matches:
                    if len(matches) != 1 or (
                        matches[0].get("status") != "paid"
                        or Decimal(str(matches[0].get("amount"))) != attempt.amount
                        or matches[0].get("currency") != attempt.currency
                    ):
                        raise ValueError("receipt_remote_mismatch")
                    charge_id = matches[0]["id"]
                else:
                    # A timeout could hide a successful POST. NEVER blindly post again.
                    if entry.status in {"posting", "unknown", "review"}:
                        raise ValueError("receipt_outcome_needs_manual_review")
                    entry.status = "posting"
                    entry.save(update_fields=["status", "updated_at"])
                    charge = client.create_offline_paid_charge(
                        reservation.hostaway_reservation_id,
                        title=marker,
                        amount=attempt.amount,
                        currency=attempt.currency,
                        description=(
                            f"Verified HyperPay {attempt.payment_brand or 'card'} receipt; "
                            "no new charge."
                        ),
                        paid_at=attempt.verified_at,
                    )
                    charge_id = charge["id"]
                entry.remote_charge_id = charge_id
                entry.status = "succeeded"
                entry.error_code = ""
                entry.completed_at = timezone.now()
                entry.save()
        except (HostawayError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
            entry.status = "unknown" if entry.status == "posting" else "review"
            entry.error_code = type(exc).__name__
            entry.save()
            alert_operations(f"receipt-review:{entry.pk}", reference=str(entry.pk))
        return {"status": entry.status}


def _dispatch_receipt(entry_id: object) -> None:
    try:
        record_payment_receipt_task.delay(str(entry_id))
    except Exception:
        logger.exception("Receipt dispatch failed")
        alert_operations(f"receipt-dispatch:{entry_id}")


def flag_confirmed_refund_for_accounting(refund_id: object) -> None:
    """Hostaway public API has no documented offline-refund creation endpoint."""
    from apps.reservations.models import RefundObligation

    refund = RefundObligation.objects.get(pk=refund_id)
    if (
        refund.status == "transferred"
        and refund.calculation.get("hyperpay_refund", {}).get("state") == "confirmed"
    ):
        alert_operations(
            f"hostaway-refund-accounting:{refund.pk}", reference=refund.public_reference
        )
