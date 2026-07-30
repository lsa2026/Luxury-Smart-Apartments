"""Provider-neutral, privacy-conscious email delivery."""

import logging
from dataclasses import dataclass, field
from smtplib import SMTPException, SMTPRecipientsRefused
from typing import Any, Protocol

from django.conf import settings
from django.core.mail import BadHeaderError, EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.html import strip_tags

from apps.notifications.models import EmailDelivery
from apps.reservations.security import mask_email

logger = logging.getLogger(__name__)


class EmailProviderError(Exception):
    def __init__(self, code: str, *, permanent: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class EmailMessageRequest:
    recipient: str
    subject: str
    template_name: str
    language: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    sent: bool
    code: str
    provider_message_id: str | None = None


class EmailProvider(Protocol):
    name: str

    def send(self, request: EmailMessageRequest) -> EmailSendResult: ...


class DisabledEmailProvider:
    name = "disabled"

    def send(self, request: EmailMessageRequest) -> EmailSendResult:
        del request
        return EmailSendResult(sent=False, code="email_delivery_disabled")


class DjangoEmailProvider:
    name = "django"

    def send(self, request: EmailMessageRequest) -> EmailSendResult:
        template_root = f"emails/{request.language}/{request.template_name}"
        context = {
            **request.context,
            "brand_name": settings.EMAIL_BRAND_NAME,
            "site_base_url": settings.SITE_BASE_URL,
        }
        try:
            text_body = render_to_string(f"emails/{request.language}/message.txt", context)
            html_body = render_to_string(f"{template_root}.html", context)
            message = EmailMultiAlternatives(
                subject=request.subject,
                body=text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[request.recipient],
            )
            message.attach_alternative(html_body, "text/html")
            sent_count = message.send(fail_silently=False)
        except (BadHeaderError, SMTPRecipientsRefused, ValueError) as exc:
            raise EmailProviderError("invalid_email_message", permanent=True) from exc
        except (TimeoutError, OSError, SMTPException) as exc:
            raise EmailProviderError("email_provider_unavailable") from exc
        if sent_count != 1:
            raise EmailProviderError("email_not_accepted")
        return EmailSendResult(sent=True, code="sent")


SUBJECTS: dict[str, dict[str, str]] = {
    "contact_confirmation": {
        "ar": "استلمنا رسالتك",
        "en": "We received your message",
    },
    "contact_admin_alert": {
        "ar": "رسالة تواصل جديدة",
        "en": "New contact message",
    },
    "booking_intent_created": {
        "ar": "تم إنشاء طلب حجز مبدئي",
        "en": "Your booking request was created",
    },
    "booking_quote_expired": {
        "ar": "انتهت صلاحية عرض السعر",
        "en": "Your quote has expired",
    },
    "booking_price_changed": {
        "ar": "تغير سعر طلب الحجز",
        "en": "Your booking price changed",
    },
    "booking_unavailable": {
        "ar": "الوحدة لم تعد متاحة",
        "en": "The property is no longer available",
    },
    "booking_ready_for_payment": {
        "ar": "طلبك جاهز لمرحلة الدفع مستقبلًا",
        "en": "Your request is ready for the future payment step",
    },
    "extension_received": {"ar": "استلمنا طلب التمديد", "en": "Extension request received"},
    "date_change_received": {
        "ar": "استلمنا طلب تغيير التواريخ",
        "en": "Date change request received",
    },
    "guest_change_received": {
        "ar": "استلمنا طلب تغيير الضيوف",
        "en": "Guest change request received",
    },
    "cancellation_received": {
        "ar": "استلمنا طلب الإلغاء",
        "en": "Cancellation request received",
    },
    "modification_approved": {
        "ar": "قُبل الطلب إداريًا",
        "en": "Request approved by operations",
    },
    "modification_rejected": {"ar": "رُفض الطلب", "en": "Request declined"},
    "modification_price_changed": {
        "ar": "تغير فرق السعر",
        "en": "Modification price difference changed",
    },
    "payment_succeeded": {"ar": "تم استلام الدفع", "en": "Payment received"},
    "reservation_confirmed": {"ar": "تم تأكيد الحجز", "en": "Reservation confirmed"},
    "reservation_creation_failed": {
        "ar": "تعذر إنشاء الحجز",
        "en": "Reservation creation failed",
    },
    "reservation_unknown": {
        "ar": "حالة الحجز قيد المراجعة",
        "en": "Reservation status under review",
    },
    "reservation_cancelled": {"ar": "تم إلغاء الحجز", "en": "Reservation cancelled"},
    "reservation_modified": {"ar": "تم تعديل الحجز", "en": "Reservation updated"},
    "daily_operations_summary": {
        "ar": "ملخص العمليات اليومي",
        "en": "Daily operations summary",
    },
}

TEMPLATE_GROUPS = {
    "contact_confirmation": "contact",
    "contact_admin_alert": "contact",
    "booking_intent_created": "booking",
    "booking_quote_expired": "booking",
    "booking_price_changed": "booking",
    "booking_unavailable": "booking",
    "booking_ready_for_payment": "booking",
    "extension_received": "modification",
    "date_change_received": "modification",
    "guest_change_received": "modification",
    "cancellation_received": "modification",
    "modification_approved": "modification",
    "modification_rejected": "modification",
    "modification_price_changed": "modification",
    "payment_succeeded": "reservation",
    "reservation_confirmed": "reservation",
    "reservation_creation_failed": "reservation",
    "reservation_unknown": "reservation",
    "reservation_cancelled": "reservation",
    "reservation_modified": "reservation",
    "daily_operations_summary": "operations",
}

MESSAGES: dict[str, dict[str, str]] = {
    "contact_confirmation": {
        "ar": "شكرًا لتواصلك معنا. استلم فريقنا رسالتك وسيراجعها.",
        "en": "Thank you for contacting us. Our team has received your message.",
    },
    "contact_admin_alert": {
        "ar": "توجد رسالة جديدة في صندوق التواصل. افتح لوحة الإدارة لمراجعتها.",
        "en": "A new message is available in the contact inbox.",
    },
    "booking_intent_created": {
        "ar": "تم إنشاء طلبك المبدئي. هذا ليس حجزًا مؤكدًا ولم تُنفذ عملية دفع.",
        "en": "Your preliminary request was created. It is not confirmed or paid.",
    },
    "booking_quote_expired": {
        "ar": "انتهت صلاحية عرض السعر. أعد التحقق للحصول على سعر وتوافر حديثين.",
        "en": "Your quote expired. Recheck for current price and availability.",
    },
    "booking_price_changed": {
        "ar": "تغير السعر أثناء إعادة التحقق، ونحتاج إلى موافقتك على العرض الجديد.",
        "en": "The price changed during revalidation and requires your approval.",
    },
    "booking_unavailable": {
        "ar": "لم تعد الوحدة متاحة للفترة المحددة. لم يُنشأ أي حجز.",
        "en": "The property is no longer available for those dates. No reservation was made.",
    },
    "booking_ready_for_payment": {
        "ar": "الطلب جاهز تقنيًا لمرحلة الدفع عند تفعيلها مستقبلًا، لكنه غير مؤكد.",
        "en": "The request is ready for the future payment step but is not confirmed.",
    },
    "extension_received": {
        "ar": "استلمنا طلب تمديد الإقامة وهو قيد المراجعة. لم يتغير الحجز بعد.",
        "en": "We received the extension request. The reservation is unchanged.",
    },
    "date_change_received": {
        "ar": "استلمنا طلب تغيير التواريخ. لم يتغير الحجز بعد.",
        "en": "We received the date-change request. The reservation is unchanged.",
    },
    "guest_change_received": {
        "ar": "استلمنا طلب تغيير عدد الضيوف. لم يتغير الحجز بعد.",
        "en": "We received the guest-change request. The reservation is unchanged.",
    },
    "cancellation_received": {
        "ar": "استلمنا طلب الإلغاء، لكن الحجز لم يُلغ بعد ولم يُنفذ استرداد.",
        "en": "We received the cancellation request; no cancellation or refund occurred.",
    },
    "modification_approved": {
        "ar": "وافق فريق العمليات على الطلب محليًا. لا يعني ذلك اكتمال تعديل Hostaway.",
        "en": "Operations approved the local request; Hostaway is not yet changed.",
    },
    "modification_rejected": {
        "ar": "تعذر قبول طلب التعديل. بقي الحجز دون تغيير.",
        "en": "The modification request was declined. The reservation is unchanged.",
    },
    "modification_price_changed": {
        "ar": "تغير فرق السعر أثناء إعادة التحقق ويحتاج إلى مراجعة جديدة.",
        "en": "The price difference changed and requires another review.",
    },
    "payment_succeeded": {
        "ar": "تم تسجيل نجاح الدفع. لا يُستخدم هذا القالب إلا بعد إثبات نجاح الدفع.",
        "en": "Payment succeeded. This template is only used after verified payment.",
    },
    "reservation_confirmed": {
        "ar": "تم تأكيد الحجز بعد استلام معرف حجز Hostaway.",
        "en": "The reservation was confirmed after receiving a Hostaway reservation ID.",
    },
    "reservation_creation_failed": {
        "ar": "تعذر إنشاء الحجز ويحتاج فريق العمليات إلى المراجعة.",
        "en": "Reservation creation failed and requires operations review.",
    },
    "reservation_unknown": {
        "ar": "حالة الحجز غير مؤكدة حاليًا وتخضع للمراجعة. لا تعاود الإجراء.",
        "en": "Reservation status is uncertain and under review. Do not retry.",
    },
    "reservation_cancelled": {
        "ar": "تم إلغاء الحجز بعد التحقق من الحالة النهائية.",
        "en": "The reservation was cancelled after final-state verification.",
    },
    "reservation_modified": {
        "ar": "تم تعديل الحجز بعد التحقق من الحالة النهائية.",
        "en": "The reservation was updated after final-state verification.",
    },
    "daily_operations_summary": {
        "ar": "يتوفر ملخص العمليات اليومي في لوحة الإدارة دون بيانات شخصية.",
        "en": "The PII-free daily operations summary is available in the admin dashboard.",
    },
}


def recipient_hmac(email: str) -> str:
    normalized = email.strip().casefold()
    return salted_hmac("email-recipient.v1", normalized).hexdigest()


def _provider_name() -> str:
    if not settings.EMAIL_DELIVERY_ENABLED:
        return DisabledEmailProvider.name
    return DjangoEmailProvider.name


def queue_email(
    *,
    message_type: str,
    recipient: str,
    recipient_source: str,
    recipient_reference: str,
    language: str,
    idempotency_key: str,
) -> EmailDelivery:
    normalized_language = language if language in {"ar", "en"} else "ar"
    subject = strip_tags(SUBJECTS[message_type][normalized_language]).strip()
    status = (
        EmailDelivery.Status.QUEUED
        if settings.EMAIL_DELIVERY_ENABLED
        else EmailDelivery.Status.DISABLED
    )
    delivery, _ = EmailDelivery.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "message_type": message_type,
            "recipient_hash": recipient_hmac(recipient),
            "recipient_masked": mask_email(recipient),
            "recipient_source": recipient_source,
            "recipient_reference": str(recipient_reference),
            "language": normalized_language,
            "subject": subject,
            "template_name": TEMPLATE_GROUPS[message_type],
            "status": status,
            "provider": _provider_name(),
            "queued_at": timezone.now(),
            "last_error_code": (
                "" if settings.EMAIL_DELIVERY_ENABLED else "email_delivery_disabled"
            ),
        },
    )
    return delivery


def _resolve_recipient(delivery: EmailDelivery) -> tuple[str, dict[str, Any]]:
    if delivery.recipient_source == "contact":
        from apps.core.models import ContactMessage

        message = ContactMessage.objects.get(pk=int(delivery.recipient_reference))
        return message.email, {"reference": str(message.pk)}
    if delivery.recipient_source == "booking_intent":
        from apps.reservations.models import BookingIntent

        intent = BookingIntent.objects.select_related("property").get(
            public_reference=delivery.recipient_reference
        )
        return intent.guest_email, {
            "reference": intent.public_reference,
            "property_name": intent.property.name_ar
            or intent.property.name_en
            or intent.property.hostaway_name,
        }
    if delivery.recipient_source == "modification":
        from apps.reservations.models import BookingModificationRequest

        modification = BookingModificationRequest.objects.select_related(
            "reservation__booking_intent",
            "reservation__property",
        ).get(public_reference=delivery.recipient_reference)
        intent = modification.reservation.booking_intent
        if intent is None:
            raise EmailProviderError("recipient_not_available", permanent=True)
        return intent.guest_email, {
            "reference": modification.public_reference,
            "property_name": modification.reservation.property.name_ar
            if modification.reservation.property
            else "",
        }
    if delivery.recipient_source == "operations":
        if not settings.OPERATIONS_EMAIL:
            raise EmailProviderError("operations_email_not_configured", permanent=True)
        return settings.OPERATIONS_EMAIL, {}
    if delivery.recipient_source == "support":
        if not settings.SUPPORT_EMAIL:
            raise EmailProviderError("support_email_not_configured", permanent=True)
        return settings.SUPPORT_EMAIL, {}
    raise EmailProviderError("unsupported_recipient_source", permanent=True)


def send_queued_email(
    delivery_id: object,
    *,
    provider: EmailProvider | None = None,
) -> EmailSendResult:
    if not settings.EMAIL_DELIVERY_ENABLED:
        EmailDelivery.objects.filter(pk=delivery_id).exclude(
            status=EmailDelivery.Status.SENT
        ).update(
            status=EmailDelivery.Status.DISABLED,
            last_error_code="email_delivery_disabled",
        )
        return EmailSendResult(False, "email_delivery_disabled")

    with transaction.atomic():
        delivery = EmailDelivery.objects.select_for_update().get(pk=delivery_id)
        if delivery.status == EmailDelivery.Status.SENT:
            return EmailSendResult(False, "already_sent", delivery.provider_message_id)
        if delivery.status == EmailDelivery.Status.SENDING:
            return EmailSendResult(False, "already_sending")
        if delivery.status in {
            EmailDelivery.Status.CANCELLED,
            EmailDelivery.Status.SKIPPED,
            EmailDelivery.Status.DISABLED,
        }:
            return EmailSendResult(False, "not_sendable")
        if delivery.attempt_count >= settings.EMAIL_MAX_RETRIES:
            return EmailSendResult(False, "retry_limit_reached")
        delivery.status = EmailDelivery.Status.SENDING
        delivery.attempt_count += 1
        delivery.save(update_fields=["status", "attempt_count", "updated_at"])

    try:
        recipient, context = _resolve_recipient(delivery)
        if recipient_hmac(recipient) != delivery.recipient_hash:
            raise EmailProviderError("recipient_integrity_failed", permanent=True)
        request = EmailMessageRequest(
            recipient=recipient,
            subject=delivery.subject,
            template_name=delivery.template_name,
            language=delivery.language,
            context={
                **context,
                "heading": delivery.subject,
                "message": MESSAGES[delivery.message_type][delivery.language],
            },
        )
        result = (provider or DjangoEmailProvider()).send(request)
    except EmailProviderError as exc:
        now = timezone.now()
        EmailDelivery.objects.filter(pk=delivery.pk).update(
            status=EmailDelivery.Status.FAILED,
            last_error_code=("permanent_" if exc.permanent else "transient_") + exc.code,
            failed_at=now,
            updated_at=now,
        )
        logger.warning("Email delivery failed code=%s delivery=%s", exc.code, delivery.pk)
        return EmailSendResult(False, exc.code)

    now = timezone.now()
    EmailDelivery.objects.filter(pk=delivery.pk).update(
        status=EmailDelivery.Status.SENT,
        provider_message_id=result.provider_message_id,
        sent_at=now,
        failed_at=None,
        last_error_code="",
        updated_at=now,
    )
    return result
