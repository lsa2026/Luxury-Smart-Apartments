"""Audited external receipt. No provider, inventory or message side effects."""

import hashlib
import json
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.access import require_operations_access
from apps.notifications.models import AuditLog
from apps.reservations.models import BookingIntent, Reservation

from .manual_references import claim_reference
from .models import OfflinePaymentReceipt, PaymentAttempt

INTERNAL_REFERENCE_PREFIX = "internal-receipt:"


def receipt_snapshot(reservation):
    data = {
        "reservation": str(reservation.pk),
        "intent": str(reservation.booking_intent_id),
        "hostaway": reservation.hostaway_reservation_id,
        "property": str(reservation.property_id),
        "arrival": str(reservation.check_in),
        "departure": str(reservation.check_out),
        "guests": reservation.guests,
        "amount": format(reservation.total_price, ".4f"),
        "currency": reservation.currency,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def record_offline_receipt(
    *,
    request,
    reservation_id,
    amount,
    currency,
    method,
    reference,
    received_at,
    snapshot_digest,
    confirmed=False,
    note="",
    lodging_only=False,
):
    require_operations_access(request)
    if not settings.MANUAL_PAYMENT_RECEIPTS_ENABLED:
        raise ValidationError("توثيق السداد اليدوي غير مفعّل.")
    if (
        confirmed is not True
        or method not in {"cash", "bank_transfer", "external_link"}
        or reference.strip().casefold().startswith(INTERNAL_REFERENCE_PREFIX)
        or len(reference) > 100
        or len(note) > 500
        or timezone.is_naive(received_at)
        or received_at > timezone.now()
    ):
        raise ValidationError("أكد القبض الفعلي وأدخل طريقة ووقتًا صحيحين؛ المرجع الخارجي اختياري.")
    value = Decimal(str(amount))
    if not value.is_finite() or value <= 0:
        raise ValidationError("مبلغ القبض غير صحيح.")
    with transaction.atomic():
        booking = Reservation.objects.select_for_update().get(pk=reservation_id)
        if snapshot_digest != receipt_snapshot(booking):
            raise ValidationError("تغيّرت بيانات الحجز؛ أعد فتحه وراجع المبلغ.")
        # Stable per reservation: a repeated blank-reference POST stays
        # idempotent. This is an internal receipt ID, never bank/gateway proof.
        receipt_reference = reference.strip() or f"{INTERNAL_REFERENCE_PREFIX}{booking.pk}"
        existing = OfflinePaymentReceipt.objects.filter(reservation=booking).first()
        if existing:
            if (
                existing.amount,
                existing.currency,
                existing.method,
                existing.reference,
                existing.received_at,
                existing.note,
            ) != (
                value,
                currency,
                method,
                receipt_reference,
                received_at,
                note.strip(),
            ):
                raise ValidationError("يوجد إيصال مختلف لهذا الحجز؛ راجع السجل دون تكرار.")
            return existing
        if (
            booking.normalized_status not in {"confirmed", "modified"}
            or value != booking.total_price
            or currency != booking.currency
            or booking.payment_status.casefold() not in {"", "unpaid", "not_paid", "unknown"}
        ):
            raise ValidationError("يلزم حجز قائم غير مسدد ومبلغ كامل مطابق لعملته.")
        if (
            booking.refund_obligations.exists()
            or booking.modification_requests.filter(
                status__in=[
                    "requested",
                    "pending_admin_approval",
                    "ready_for_hostaway",
                    "awaiting_payment",
                    "processing",
                ]
            ).exists()
        ):
            raise ValidationError("احسم تعديل الحجز أو الاسترداد القائم قبل توثيق القبض.")
        if booking.booking_intent_id:
            intent = BookingIntent.objects.select_for_update().get(pk=booking.booking_intent_id)
            if (
                PaymentAttempt.objects.filter(
                    booking_intent=intent,
                    status__in=[
                        "created",
                        "pending",
                        "unknown",
                        "review",
                        "succeeded",
                        "partially_refunded",
                        "refunded",
                    ],
                ).exists()
                or booking.loyalty_state.get("hold")
                or booking.loyalty_state.get("redemption")
            ):
                raise ValidationError("يوجد تحصيل أو محاولة دفع أو نقاط؛ يلزم مراجعتها أولًا.")
        claim = claim_reference(booking, receipt_reference)
        if claim.payment_attempt_id:
            raise ValidationError("مرجع القبض موثق سابقًا؛ راجع الإيصال الأصلي.")
        receipt = OfflinePaymentReceipt.objects.create(
            reservation=booking,
            amount=value,
            currency=currency,
            method=method,
            reference=receipt_reference,
            note=note.strip(),
            received_at=received_at,
            recorded_by=request.user,
            snapshot_digest=snapshot_digest,
        )
        if booking.booking_intent_id:
            payment = PaymentAttempt.objects.create(
                booking_intent_id=booking.booking_intent_id,
                provider="manual",
                provider_reference=receipt_reference,
                provider_payment_id=f"manual:{receipt.pk}",
                idempotency_key=f"offline:{receipt.pk}",
                amount=value,
                currency=currency,
                status="succeeded",
                verified_at=received_at,
                payment_brand=method,
            )
            receipt.payment_attempt = payment
            receipt.save(update_fields=["payment_attempt"])
            claim.payment_attempt = payment
            claim.save(update_fields=["payment_attempt"])
            BookingIntent.objects.filter(pk=booking.booking_intent_id).update(
                status=BookingIntent.Status.COMPLETED, updated_at=timezone.now()
            )
        # Row lock is shared with checkout creation. A live checkout blocks the
        # receipt above; links without an attempted collection can be revoked safely.
        booking.payment_requests.filter(status="open").update(status="revoked")
        booking.payment_status = "paid"
        if lodging_only is True:
            booking.loyalty_state["manual_lodging_price"] = {
                "total": str(booking.total_price),
                "reference": receipt_reference,
                "operator": request.user.pk,
                "at": timezone.now().isoformat(),
            }
        booking.save(update_fields=["payment_status", "loyalty_state", "updated_at"])
        AuditLog.objects.create(
            actor_user=request.user,
            action="payment.offline_received",
            object_type="OfflinePaymentReceipt",
            object_reference=str(receipt.pk),
            summary="توثيق قبض خارجي فعلي دون تحصيل جديد أو تأكيد آلي من بوابة الدفع",
            metadata={
                "reservation": str(booking.pk),
                "method": method,
                "amount": str(value),
                "currency": currency,
                "received_at": received_at.isoformat(),
                "external_reference_supplied": bool(reference.strip()),
            },
        )
        from apps.loyalty.site_adapter import schedule_reconciliation

        transaction.on_commit(lambda: schedule_reconciliation(booking.pk), robust=True)
        return receipt
