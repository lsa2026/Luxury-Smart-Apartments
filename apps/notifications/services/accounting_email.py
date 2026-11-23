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
from apps.reservations.models import BookingModificationRequest, Reservation
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
            return _existing(current)
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
    from .ultramsg import _manual_payment_message

    return _deliver(delivery, lambda: _manual_payment_message(reservation))


def modification_key(modification):
    return "modification-accounting-email:" + str(modification.pk)


def status_for_modification(modification):
    return EmailDelivery.objects.filter(idempotency_key=modification_key(modification)).first() or (
        WhatsAppDelivery.objects.filter(modification_request=modification).first()
    )


def _existing(current):
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


@sensitive_variables()
def send_modification_payment_link_request(*, modification_id):
    with transaction.atomic():
        modification = BookingModificationRequest.objects.select_for_update().get(
            pk=modification_id
        )
        from apps.reservations.services.owner_settlement import owner_change_blocker, settlement_for

        owner_priced = modification.quote_snapshot.get("owner_final_total") is not None
        if owner_priced:
            blocker = owner_change_blocker(modification, completed=True)
            if blocker or modification.status != BookingModificationRequest.Status.COMPLETED:
                return AccountingDeliveryResult(blocker or "hostaway_confirmation_required", None)
            if settlement_for(modification.reservation, modification.new_total).due <= 0:
                return AccountingDeliveryResult("nothing_due", None)
        elif modification.price_difference <= 0:
            raise ValueError("A payment link is only needed for a positive difference.")
        legacy = WhatsAppDelivery.objects.filter(modification_request=modification).first()
        if legacy is not None:
            return AccountingDeliveryResult("legacy_review_required", legacy)
        current = EmailDelivery.objects.filter(
            idempotency_key=modification_key(modification)
        ).first()
        if current is not None:
            return _existing(current)
        enabled = (
            settings.EMAIL_DELIVERY_ENABLED
            and settings.OPERATIONS_EMAIL.strip().casefold() == BUSINESS_EMAIL
        )
        delivery = EmailDelivery.objects.create(
            message_type="modification_payment_link_request",
            recipient_hash=recipient_hmac(BUSINESS_EMAIL),
            recipient_masked=mask_email(BUSINESS_EMAIL),
            recipient_source="operations",
            recipient_reference=str(modification.pk),
            language="ar",
            subject="طلب إنشاء رابط دفع لفرق تعديل حجز",
            template_name="accounting_modification_link",
            status=EmailDelivery.Status.SENDING if enabled else EmailDelivery.Status.DISABLED,
            provider="django",
            idempotency_key=modification_key(modification),
            attempt_count=1 if enabled else 0,
            queued_at=timezone.now(),
            last_error_code="" if enabled else "accounting_email_disabled",
        )
        if not enabled:
            return AccountingDeliveryResult("email_disabled", delivery)
    # Read nullable relations only after the modification lock is released.
    modification = BookingModificationRequest.objects.select_related(
        "reservation", "reservation__booking_intent", "reservation__property"
    ).get(pk=modification.pk)
    from .ultramsg import _modification_payment_message

    return _deliver(delivery, lambda: _modification_payment_message(modification))


@sensitive_variables()
def _deliver(delivery, render):
    try:
        body = render()
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
