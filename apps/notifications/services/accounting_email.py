"""One manual-link operations email, never a payment or guest message.

The permanent fence is committed before SMTP. Ambiguity is retained without
automatic retry or fallback. Existing UltraMsg attempts are never adopted.
"""

from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.notifications.models import EmailDelivery, WhatsAppDelivery
from apps.reservations.models import Reservation
from apps.reservations.security import mask_email

from .email import recipient_hmac

BUSINESS_EMAIL = "saeed@luxurysmartapartments.com"


@dataclass(frozen=True, slots=True)
class AccountingDeliveryResult:
    code: str
    delivery: EmailDelivery | WhatsAppDelivery | None


def key(reservation):
    return "manual-accounting-email:" + str(reservation.pk)


def status_for(reservation):
    return EmailDelivery.objects.filter(idempotency_key=key(reservation)).first() or (
        WhatsAppDelivery.objects.filter(
            reservation=reservation, message_type="manual_payment_link_request"
        ).first()
    )


@sensitive_variables()
def send_manual_payment_link_request(*, reservation_id):
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().filter(pk=reservation_id).first()
        if reservation is None:
            raise ValueError("reservation_missing")
        legacy = WhatsAppDelivery.objects.filter(reservation=reservation).first()
        if legacy is not None:
            return AccountingDeliveryResult("legacy_review_required", legacy)
        current = EmailDelivery.objects.filter(idempotency_key=key(reservation)).first()
        if current is not None:
            code = (
                "email_already_sent"
                if current.status == EmailDelivery.Status.SENT
                else "email_disabled"
                if current.status == EmailDelivery.Status.DISABLED
                else "email_unknown"
                if current.last_error_code == "accounting_email_outcome_unknown"
                or current.status == EmailDelivery.Status.SENDING
                else "email_previously_failed"
            )
            return AccountingDeliveryResult(code, current)
        enabled = (
            settings.EMAIL_DELIVERY_ENABLED
            and settings.OPERATIONS_EMAIL.strip().casefold() == BUSINESS_EMAIL
        )
        delivery = EmailDelivery.objects.create(
            message_type="manual_payment_link_request",
            recipient_hash=recipient_hmac(BUSINESS_EMAIL),
            recipient_masked=mask_email(BUSINESS_EMAIL),
            recipient_source="operations",
            recipient_reference=str(reservation.pk),
            language="ar",
            subject="طلب إنشاء رابط دفع يدوي",
            template_name="accounting_manual_link",
            status=EmailDelivery.Status.SENDING if enabled else EmailDelivery.Status.DISABLED,
            provider="django",
            idempotency_key=key(reservation),
            attempt_count=1 if enabled else 0,
            queued_at=timezone.now(),
            last_error_code="" if enabled else "accounting_email_disabled",
        )
        if not enabled:
            return AccountingDeliveryResult("email_disabled", delivery)
    try:
        # Preserve the reviewed operations content; this is only a renderer.
        from .ultramsg import _manual_payment_message

        body = _manual_payment_message(reservation)
        message = EmailMessage(
            subject=delivery.subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[BUSINESS_EMAIL],
        )
    except Exception:
        EmailDelivery.objects.filter(pk=delivery.pk).update(
            status=EmailDelivery.Status.FAILED,
            failed_at=timezone.now(),
            last_error_code="accounting_email_build_failed",
        )
        delivery.refresh_from_db()
        return AccountingDeliveryResult("email_failed", delivery)
    try:
        accepted = message.send(fail_silently=False) == 1
    except Exception:
        accepted = False
    if not accepted:
        # SMTP may have accepted the message before a timeout/response loss.
        # Never queue this row, use transient_* codes, or retry through UltraMsg.
        EmailDelivery.objects.filter(pk=delivery.pk).update(
            status=EmailDelivery.Status.FAILED,
            failed_at=timezone.now(),
            last_error_code="accounting_email_outcome_unknown",
        )
        delivery.refresh_from_db()
        return AccountingDeliveryResult("email_unknown", delivery)
    EmailDelivery.objects.filter(pk=delivery.pk).update(
        status=EmailDelivery.Status.SENT,
        sent_at=timezone.now(),
        last_error_code="",
    )
    delivery.refresh_from_db()
    return AccountingDeliveryResult("email_sent", delivery)
