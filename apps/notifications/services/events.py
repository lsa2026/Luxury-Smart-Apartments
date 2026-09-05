"""Explicit, idempotent secondary event dispatch."""

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError
from django.urls import reverse

from apps.notifications.models import Notification
from apps.notifications.services.email import queue_email

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EventDefinition:
    notification_type: str
    severity: str
    title_ar: str
    title_en: str
    message_ar: str
    message_en: str


EVENTS = {
    "contact_message.created": EventDefinition(
        Notification.Type.CONTACT_MESSAGE_RECEIVED,
        Notification.Severity.INFO,
        "رسالة تواصل جديدة",
        "New contact message",
        "وصلت رسالة تواصل جديدة إلى صندوق الإدارة.",
        "A new contact message reached the operations inbox.",
    ),
    "booking_intent.created": EventDefinition(
        Notification.Type.BOOKING_INTENT_CREATED,
        Notification.Severity.INFO,
        "طلب حجز مبدئي جديد",
        "New booking request",
        "أُنشئ طلب حجز مبدئي غير مؤكد.",
        "A non-confirmed booking request was created.",
    ),
    "booking_intent.price_changed": EventDefinition(
        Notification.Type.BOOKING_PRICE_CHANGED,
        Notification.Severity.WARNING,
        "تغير سعر طلب حجز",
        "Booking price changed",
        "تغير السعر أثناء إعادة التحقق.",
        "Price changed during revalidation.",
    ),
    "booking_intent.unavailable": EventDefinition(
        Notification.Type.BOOKING_UNAVAILABLE,
        Notification.Severity.WARNING,
        "الوحدة لم تعد متاحة",
        "Property became unavailable",
        "تعذر متابعة طلب الحجز بسبب التوافر.",
        "The booking request could not continue due to availability.",
    ),
    "modification_request.created": EventDefinition(
        Notification.Type.MODIFICATION_REQUESTED,
        Notification.Severity.INFO,
        "طلب تعديل حجز",
        "Reservation modification requested",
        "وصل طلب تعديل محلي وهو بانتظار المراجعة.",
        "A local modification request is awaiting review.",
    ),
    "cancellation_request.created": EventDefinition(
        Notification.Type.CANCELLATION_REQUESTED,
        Notification.Severity.WARNING,
        "طلب إلغاء",
        "Cancellation requested",
        "وصل طلب إلغاء محلي ولم يُلغ الحجز بعد.",
        "A local cancellation request was received; the reservation is unchanged.",
    ),
    "refund.due": EventDefinition(
        Notification.Type.REFUND_DUE,
        Notification.Severity.WARNING,
        "مبلغ مستحق للنزيل",
        "Refund owed to a guest",
        "طُبِّق تغيير على الحجز ونتج عنه مبلغ يجب تحويله للنزيل.",
        "A booking change was applied and left an amount to transfer to the guest.",
    ),
    "hostaway_sync.failed": EventDefinition(
        Notification.Type.HOSTAWAY_SYNC_FAILED,
        Notification.Severity.ERROR,
        "فشل مزامنة Hostaway",
        "Hostaway sync failed",
        "فشل تشغيل مزامنة ويحتاج إلى مراجعة.",
        "A sync run failed and requires review.",
    ),
    "webhook_event.failed": EventDefinition(
        Notification.Type.WEBHOOK_FAILED,
        Notification.Severity.ERROR,
        "فشل معالجة Webhook",
        "Webhook processing failed",
        "تعذر معالجة حدث منقح.",
        "A sanitized event could not be processed.",
    ),
    "reservation.create_unknown": EventDefinition(
        Notification.Type.RESERVATION_UNKNOWN,
        Notification.Severity.ERROR,
        "حالة إنشاء حجز غير مؤكدة",
        "Reservation creation status unknown",
        "تحتاج العملية إلى مصالحة يدوية.",
        "The operation requires manual reconciliation.",
    ),
    "booking_quote.expired": EventDefinition(
        Notification.Type.SYSTEM_WARNING,
        Notification.Severity.INFO,
        "انتهى عرض سعر",
        "Booking quote expired",
        "انتهت صلاحية عرض سعر محلي.",
        "A local booking quote expired.",
    ),
}


def dispatch_event(
    event_name: str,
    *,
    event_key: str,
    related_object_type: str,
    related_object_reference: str,
    action_url: str = "",
    email: dict[str, Any] | None = None,
) -> Notification | None:
    """Create secondary records without breaking the primary business operation."""
    definition = EVENTS.get(event_name)
    if definition is None or not settings.NOTIFICATIONS_ENABLED:
        return None
    try:
        notification, _ = Notification.objects.get_or_create(
            idempotency_key=f"event:{event_key}"[:100],
            defaults={
                "notification_type": definition.notification_type,
                "audience_type": Notification.Audience.ADMIN,
                "title_ar": definition.title_ar,
                "title_en": definition.title_en,
                "message_ar": definition.message_ar,
                "message_en": definition.message_en,
                "action_url": action_url,
                "severity": definition.severity,
                "related_object_type": related_object_type,
                "related_object_reference": str(related_object_reference)[:100],
            },
        )
        if email:
            queue_email(**email)
        return notification
    except (DatabaseError, KeyError, ObjectDoesNotExist, ValueError) as exc:
        logger.error("Secondary event failed event=%s code=%s", event_name, type(exc).__name__)
        return None


def handle_contact_created(message_id: int, *, email: str, language: str) -> None:
    notification = dispatch_event(
        "contact_message.created",
        event_key=f"contact:{message_id}",
        related_object_type="ContactMessage",
        related_object_reference=str(message_id),
        action_url=f"/admin/core/contactmessage/{message_id}/change/",
    )
    if notification is None:
        return
    try:
        if settings.CONTACT_NOTIFICATION_EMAIL_ENABLED:
            queue_email(
                message_type="contact_confirmation",
                recipient=email,
                recipient_source="contact",
                recipient_reference=str(message_id),
                language=language,
                idempotency_key=f"contact-confirmation:{message_id}",
            )
        if settings.ADMIN_NOTIFICATION_EMAIL_ENABLED and settings.OPERATIONS_EMAIL:
            queue_email(
                message_type="contact_admin_alert",
                recipient=settings.OPERATIONS_EMAIL,
                recipient_source="operations",
                recipient_reference="operations",
                language="ar",
                idempotency_key=f"contact-admin-alert:{message_id}",
            )
    except (DatabaseError, KeyError, ObjectDoesNotExist, ValueError) as exc:
        logger.error("Contact email queue failed code=%s", type(exc).__name__)


def handle_booking_intent_created(intent_id: object) -> None:
    from apps.reservations.models import BookingIntent

    try:
        intent = BookingIntent.objects.get(pk=intent_id)
        notification = dispatch_event(
            "booking_intent.created",
            event_key=f"booking-intent:{intent.public_reference}",
            related_object_type="BookingIntent",
            related_object_reference=intent.public_reference,
            action_url=f"/admin/reservations/bookingintent/{intent.pk}/change/",
        )
        if notification is not None and settings.BOOKING_NOTIFICATION_EMAIL_ENABLED:
            queue_email(
                message_type="booking_intent_created",
                recipient=intent.guest_email,
                recipient_source="booking_intent",
                recipient_reference=intent.public_reference,
                language=intent.language,
                idempotency_key=f"booking-intent-created:{intent.public_reference}",
            )
    except (DatabaseError, KeyError, ObjectDoesNotExist, ValueError) as exc:
        logger.error("Booking event failed code=%s", type(exc).__name__)


def handle_modification_created(modification_id: object) -> None:
    """Record the internal event without emailing an unconfirmed customer request."""
    from apps.reservations.models import BookingModificationRequest

    try:
        modification = BookingModificationRequest.objects.select_related(
            "reservation__booking_intent"
        ).get(pk=modification_id)
        is_cancellation = (
            modification.request_type == BookingModificationRequest.RequestType.CANCEL_RESERVATION
        )
        event_name = (
            "cancellation_request.created" if is_cancellation else "modification_request.created"
        )
        dispatch_event(
            event_name,
            event_key=f"modification:{modification.public_reference}",
            related_object_type="BookingModificationRequest",
            related_object_reference=modification.public_reference,
            action_url=(
                f"/admin/reservations/bookingmodificationrequest/{modification.pk}/change/"
            ),
        )
    except (DatabaseError, KeyError, ValueError) as exc:
        logger.error("Modification event failed code=%s", type(exc).__name__)


def handle_refund_due(obligation_id: object) -> None:
    """Announce a debt to a guest. Contact details stay in the admin record."""
    from apps.reservations.models import RefundObligation

    obligation = (
        RefundObligation.objects.filter(pk=obligation_id)
        .select_related("reservation")
        .first()
    )
    if obligation is None:
        return
    dispatch_event(
        "refund.due",
        event_key=f"refund.due:{obligation.public_reference}",
        related_object_type="RefundObligation",
        related_object_reference=obligation.public_reference,
        action_url=reverse(
            "admin:reservations_refundobligation_change",
            args=[obligation.pk],
        ),
    )


def handle_reservation_confirmed(reservation_id: object) -> None:
    from apps.reservations.models import Reservation

    try:
        reservation = Reservation.objects.select_related("booking_intent").get(
            pk=reservation_id
        )
        intent = reservation.booking_intent
        if (
            intent is None
            or reservation.normalized_status != Reservation.Status.CONFIRMED
            or not settings.BOOKING_NOTIFICATION_EMAIL_ENABLED
        ):
            return
        queue_email(
            message_type="reservation_confirmed",
            recipient=intent.guest_email,
            recipient_source="reservation",
            recipient_reference=reservation.public_reference,
            language=intent.language,
            idempotency_key=f"reservation-confirmed:{reservation.public_reference}",
        )
    except (DatabaseError, KeyError, ObjectDoesNotExist, ValueError) as exc:
        logger.error("Reservation confirmation email failed code=%s", type(exc).__name__)


def handle_modification_completed(modification_id: object) -> None:
    """Queue one final customer email only after Hostaway state is reconciled."""
    from apps.reservations.models import BookingModificationRequest

    try:
        modification = BookingModificationRequest.objects.select_related(
            "reservation__booking_intent"
        ).get(pk=modification_id)
        intent = modification.reservation.booking_intent
        if (
            intent is None
            or modification.status != BookingModificationRequest.Status.COMPLETED
            or not settings.MODIFICATION_NOTIFICATION_EMAIL_ENABLED
        ):
            return
        is_cancellation = (
            modification.request_type
            == BookingModificationRequest.RequestType.CANCEL_RESERVATION
        )
        queue_email(
            message_type=("reservation_cancelled" if is_cancellation else "reservation_modified"),
            recipient=intent.guest_email,
            recipient_source="modification",
            recipient_reference=modification.public_reference,
            language=intent.language,
            idempotency_key=f"modification-completed:{modification.public_reference}",
        )
    except (DatabaseError, KeyError, ObjectDoesNotExist, ValueError) as exc:
        logger.error("Modification completion email failed code=%s", type(exc).__name__)
