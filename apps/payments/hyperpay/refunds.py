"""HyperPay Backoffice refunds with local idempotency and owner review."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.payments.models import PaymentAttempt
from apps.reservations.models import RefundObligation

from .client import HyperPayClient
from .exceptions import (
    HyperPayAuthenticationError,
    HyperPayConfigurationError,
    HyperPayConnectionError,
    HyperPayRefundError,
    HyperPayResponseError,
)
from .result_codes import HyperPayStatus, map_result_code
from .service import HYPERPAY_PROVIDER, format_hyperpay_amount


@dataclass(frozen=True, slots=True)
class RefundOutcome:
    audit_action: str
    audit_summary: str
    audit_metadata: dict[str, str]
    message: str


class HyperPayRefundService:
    """Send a refund only after an authorized owner explicitly confirms it."""

    def __init__(self, client: HyperPayClient | None = None) -> None:
        self.client = client

    @staticmethod
    def is_available() -> bool:
        return bool(
            settings.HYPERPAY_ENABLED
            and settings.HYPERPAY_REFUNDS_ENABLED
            and (
                settings.HYPERPAY_ENVIRONMENT != "production"
                or settings.HYPERPAY_REFUNDS_PRODUCTION_ENABLED
            )
        )

    def submit(self, refund: RefundObligation, *, operator: object) -> RefundOutcome:
        if not self.is_available():
            raise HyperPayRefundError("hyperpay_refunds_not_enabled")

        with transaction.atomic():
            locked, payment = self._prepare(refund.pk)
            self._record_submission(locked, payment)

        try:
            client = self.client or HyperPayClient()
            owns_client = self.client is None
            try:
                document = client.refund_payment(
                    payment.provider_payment_id or "",
                    {
                        "entityId": settings.HYPERPAY_ENTITY_ID,
                        "amount": format_hyperpay_amount(locked.amount),
                        "currency": locked.currency,
                        "paymentType": "RF",
                    },
                )
            finally:
                if owns_client:
                    client.close()
        except HyperPayConnectionError as exc:
            self._mark_unknown(locked.pk, "connection_result_unknown")
            raise HyperPayRefundError("refund_result_unknown") from exc
        except (HyperPayAuthenticationError, HyperPayConfigurationError) as exc:
            self._restore_due(locked.pk, exc.code)
            raise HyperPayRefundError(exc.code) from exc
        except HyperPayResponseError as exc:
            self._restore_due(locked.pk, exc.code)
            raise HyperPayRefundError(exc.code) from exc

        return self._record_response(locked.pk, payment, document, operator)

    @staticmethod
    def _prepare(refund_id: object) -> tuple[RefundObligation, PaymentAttempt]:
        refund = (
            RefundObligation.objects.select_for_update()
            .select_related("reservation__booking_intent")
            .get(pk=refund_id)
        )
        if refund.status != RefundObligation.Status.DUE or refund.amount <= Decimal("0"):
            raise HyperPayRefundError("refund_not_available")
        if refund.currency != "SAR":
            raise HyperPayRefundError("refund_currency_not_supported")
        intent = refund.reservation.booking_intent
        if intent is None:
            raise HyperPayRefundError("original_payment_not_found")
        payment = (
            PaymentAttempt.objects.select_for_update()
            .filter(
                booking_intent=intent,
                modification_request__isnull=True,
                provider=HYPERPAY_PROVIDER,
                status=PaymentAttempt.Status.SUCCEEDED,
            )
            .exclude(provider_payment_id__isnull=True)
            .exclude(provider_payment_id="")
            .order_by("-verified_at", "-created_at")
            .first()
        )
        if payment is None:
            raise HyperPayRefundError("original_payment_not_found")

        previously_refunded = Decimal("0")
        for prior in refund.reservation.refund_obligations.filter(
            status__in=[RefundObligation.Status.PROCESSING, RefundObligation.Status.TRANSFERRED]
        ).exclude(pk=refund.pk):
            detail = (
                prior.calculation.get("hyperpay_refund", {})
                if isinstance(prior.calculation, dict)
                else {}
            )
            if detail.get("original_payment_id") == payment.provider_payment_id:
                previously_refunded += prior.amount
        if refund.amount > payment.amount - previously_refunded:
            raise HyperPayRefundError("refund_amount_exceeds_original_payment")
        return refund, payment

    @staticmethod
    def _record_submission(refund: RefundObligation, payment: PaymentAttempt) -> None:
        calculation = deepcopy(refund.calculation) if isinstance(refund.calculation, dict) else {}
        calculation["hyperpay_refund"] = {
            "state": "submitting",
            "original_payment_id": payment.provider_payment_id,
            "amount": format(refund.amount, "f"),
            "currency": refund.currency,
            "submitted_at": timezone.now().isoformat(),
        }
        refund.status = RefundObligation.Status.PROCESSING
        refund.calculation = calculation
        refund.save(update_fields=["status", "calculation", "updated_at"])

    @staticmethod
    def _restore_due(refund_id: object, error_code: str) -> None:
        with transaction.atomic():
            refund = RefundObligation.objects.select_for_update().get(pk=refund_id)
            calculation = (
                deepcopy(refund.calculation) if isinstance(refund.calculation, dict) else {}
            )
            detail = calculation.get("hyperpay_refund", {})
            if isinstance(detail, dict):
                detail.update(
                    {
                        "state": "rejected",
                        "error": error_code,
                        "updated_at": timezone.now().isoformat(),
                    }
                )
                calculation["hyperpay_refund"] = detail
            refund.status = RefundObligation.Status.DUE
            refund.calculation = calculation
            refund.save(update_fields=["status", "calculation", "updated_at"])

    @staticmethod
    def _mark_unknown(refund_id: object, reason: str) -> None:
        with transaction.atomic():
            refund = RefundObligation.objects.select_for_update().get(pk=refund_id)
            calculation = (
                deepcopy(refund.calculation) if isinstance(refund.calculation, dict) else {}
            )
            detail = calculation.get("hyperpay_refund", {})
            if isinstance(detail, dict):
                detail.update(
                    {"state": "unknown", "error": reason, "updated_at": timezone.now().isoformat()}
                )
                calculation["hyperpay_refund"] = detail
            refund.calculation = calculation
            refund.save(update_fields=["calculation", "updated_at"])

    @staticmethod
    def _record_response(
        refund_id: object,
        payment: PaymentAttempt,
        document: dict[str, Any],
        operator: object,
    ) -> RefundOutcome:
        result = document.get("result")
        code = result.get("code") if isinstance(result, dict) else ""
        description = result.get("description") if isinstance(result, dict) else ""
        status = map_result_code(code)
        provider_refund_id = document.get("id") if isinstance(document.get("id"), str) else ""
        with transaction.atomic():
            refund = RefundObligation.objects.select_for_update().get(pk=refund_id)
            calculation = (
                deepcopy(refund.calculation) if isinstance(refund.calculation, dict) else {}
            )
            detail = calculation.get("hyperpay_refund", {})
            if not isinstance(detail, dict):
                raise HyperPayRefundError("refund_record_invalid")
            detail.update(
                {
                    "provider_refund_id": provider_refund_id[:255],
                    "result_code": code[:100] if isinstance(code, str) else "",
                    "result_description": description[:255] if isinstance(description, str) else "",
                    "updated_at": timezone.now().isoformat(),
                }
            )
            calculation["hyperpay_refund"] = detail
            if status is HyperPayStatus.SUCCESS:
                detail["state"] = "confirmed"
                refund.status = RefundObligation.Status.TRANSFERRED
                refund.transfer_reference = provider_refund_id[:100]
                refund.transferred_at = timezone.now()
                refund.transferred_by = operator if getattr(operator, "pk", None) else None
                refund.calculation = calculation
                refund.save(
                    update_fields=[
                        "status", "transfer_reference", "transferred_at", "transferred_by",
                        "calculation", "updated_at",
                    ]
                )
                total = HyperPayRefundService._total_confirmed(payment)
                payment.status = (
                    PaymentAttempt.Status.REFUNDED
                    if total >= payment.amount
                    else PaymentAttempt.Status.PARTIALLY_REFUNDED
                )
                payment.save(update_fields=["status", "updated_at"])
                transaction.on_commit(
                    lambda refund_id=refund.pk: _queue_guest_refund_notice(refund_id),
                    robust=True,
                )
                return RefundOutcome(
                    "refund.hyperpay_completed",
                    "HyperPay confirmed the refund.",
                    {
                        "amount": format(refund.amount, "f"), "currency": refund.currency,
                        "provider_refund_id": provider_refund_id[:255],
                    },
                    "أكدت HyperPay الاسترداد وسُجلت العملية في مركز التشغيل.",
                )
            if status is HyperPayStatus.PENDING:
                detail["state"] = "provider_pending"
                refund.calculation = calculation
                refund.save(update_fields=["calculation", "updated_at"])
                transaction.on_commit(
                    lambda refund_id=refund.pk: _queue_guest_refund_notice(refund_id),
                    robust=True,
                )
                return RefundOutcome(
                    "refund.hyperpay_submitted",
                    "HyperPay accepted the refund and it is pending.",
                    {
                        "amount": format(refund.amount, "f"), "currency": refund.currency,
                        "provider_refund_id": provider_refund_id[:255],
                    },
                    "استلمت HyperPay طلب الاسترداد وهو قيد المعالجة؛ لا تعِد إرساله.",
                )
            detail["state"] = "rejected"
            refund.status = RefundObligation.Status.DUE
            refund.calculation = calculation
            refund.save(update_fields=["status", "calculation", "updated_at"])
            return RefundOutcome(
                "refund.hyperpay_failed",
                "HyperPay rejected the refund; it remains due locally.",
                {"result_code": code[:100] if isinstance(code, str) else ""},
                "رفضت HyperPay الاسترداد؛ لم يُسجل أي تحويل ويمكن مراجعة السبب قبل المحاولة لاحقًا.",
            )

    @staticmethod
    def _total_confirmed(payment: PaymentAttempt) -> Decimal:
        total = Decimal("0")
        for obligation in RefundObligation.objects.filter(
            reservation__booking_intent=payment.booking_intent,
            status=RefundObligation.Status.TRANSFERRED,
        ):
            detail = (
                obligation.calculation.get("hyperpay_refund", {})
                if isinstance(obligation.calculation, dict)
                else {}
            )
            if detail.get("original_payment_id") == payment.provider_payment_id:
                total += obligation.amount
        return total


def _queue_guest_refund_notice(refund_id: object) -> None:
    """Keep notification failure out of the payment-provider transaction path."""
    try:
        from apps.notifications.services.events import handle_refund_submitted

        handle_refund_submitted(refund_id)
    except (ImportError, ValueError):
        # The refund is already recorded. Logging here is intentionally avoided
        # because email delivery has its own auditable retry record.
        return
