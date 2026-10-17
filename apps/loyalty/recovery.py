"""Owner-assisted recovery of uncertain full-points creates, by read only."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.access import require_operations_owner
from apps.notifications.models import AuditLog, Notification
from apps.reservations.models import BookingIntent, HostawayReservationOperation, Reservation

from .services import posting_transaction, redeem
from .settlements import eligible


def alert_uncertain(booking):
    Notification.objects.get_or_create(
        idempotency_key=f"loyalty-uncertain:{booking.pk}",
        defaults={
            "notification_type": Notification.Type.RESERVATION_UNKNOWN,
            "audience_type": Notification.Audience.ADMIN,
            "title_ar": "يلزم حسم حجز ممول بالنقاط",
            "title_en": "A points-funded booking needs verification",
            "message_ar": "النتيجة غير مؤكدة. النقاط محفوظة؛ تحقق من الحجز دون إعادة إنشائه.",
            "message_en": "Outcome uncertain. Points remain reserved. Verify without recreating.",
            "severity": Notification.Severity.WARNING,
            "related_object_type": "Reservation",
            "related_object_reference": str(booking.pk),
            "action_url": "/admin/loyalty/",
        },
    )


def recover_points_booking(
    operator, reservation_id, *, external_id, reference, guest_identity_confirmed, client
):
    require_operations_owner(operator)
    if guest_identity_confirmed is not True or not reference.strip() or external_id <= 0:
        raise ValidationError("يلزم تحقق هوية الضيف ومرجع المراجعة ورقم الحجز الصحيح.")
    booking = Reservation.objects.select_related("property", "booking_intent__customer").get(
        pk=reservation_id
    )
    from .launch import booking_allowed

    if not booking_allowed(booking):
        raise ValidationError("الحجز خارج نطاق التجربة الخاصة.")
    if not eligible(booking) or not booking.loyalty_state.get("points_confirmation_started"):
        raise ValidationError("لا توجد نتيجة حجز نقاط معلقة.")
    # No DB financial lock while reading the provider. Never re-send creation.
    snapshot = client.get_reservation(external_id)
    from apps.integrations.hostaway.reservation_validators import (
        normalize_hostaway_reservation_status,
    )

    if (
        snapshot.reservation_id != external_id
        or snapshot.listing_map_id != booking.property.hostaway_listing_map_id
        or snapshot.check_in != booking.check_in
        or snapshot.check_out != booking.check_out
        or snapshot.guests != booking.guests
        or snapshot.currency != booking.currency
        or snapshot.total_price != booking.total_price
        or normalize_hostaway_reservation_status(snapshot.status) not in Reservation.ACTIVE_STATUSES
    ):
        raise ValidationError("تفاصيل الحجز الخارجي لا تطابق الحجز المطلوب.")
    with posting_transaction():
        locked = Reservation.objects.select_for_update().get(pk=booking.pk)
        if locked.hostaway_reservation_id not in (None, external_id):
            raise ValidationError("الحجز مرتبط برقم خارجي مختلف.")
        if locked.loyalty_state.get("redemption"):
            return {"status": "already_confirmed"}
        held = locked.loyalty_state.get("hold", {})
        if held.get("member") != booking.booking_intent.customer.member.pk:
            raise ValidationError("حساب النقاط لا يطابق صاحب الحجز.")
        from .valuation import total_sar

        if not held or Decimal(held["value"]) != total_sar(locked):
            raise ValidationError("هذا المسار مخصص للحجز المغطى كاملًا بالنقاط.")
        if (
            locked.check_in != snapshot.check_in
            or locked.check_out != snapshot.check_out
            or locked.total_price != snapshot.total_price
            or locked.guests != snapshot.guests
        ):
            raise ValidationError("تغير الحجز أثناء المراجعة؛ أعد التحقق.")
        locked.hostaway_reservation_id = external_id
        locked.normalized_status = Reservation.Status.CONFIRMED
        locked.hostaway_status = snapshot.status
        locked.confirmed_at = timezone.now()
        locked.payment_status = "paid_with_points"
        locked.loyalty_state["points_recovery"] = {
            "operator": operator.pk,
            "reference": reference.strip(),
            "at": timezone.now().isoformat(),
            "external_id": external_id,
        }
        locked.save()
        redeem(booking.booking_intent.customer, f"points-confirm:{booking.pk}", booking.pk)
        BookingIntent.objects.filter(pk=booking.booking_intent_id).update(status="completed")
        HostawayReservationOperation.objects.filter(
            reservation=locked, operation_type="create_reservation"
        ).update(
            status="succeeded",
            hostaway_reservation_id=external_id,
            completed_at=timezone.now(),
            error_code="",
        )
        AuditLog.objects.create(
            actor_user=operator,
            action="loyalty.points_recovery",
            object_type="Reservation",
            object_reference=str(booking.pk),
            summary="حسم حجز نقاط بقراءة حجز موجود دون إنشائه أو تحصيل جديد",
            metadata={"reference": reference.strip(), "external_id": external_id},
        )
        Notification.objects.filter(idempotency_key=f"loyalty-uncertain:{booking.pk}").update(
            status=Notification.Status.ARCHIVED
        )
        from django.db import transaction

        from apps.notifications.services.events import handle_reservation_confirmed

        transaction.on_commit(lambda: handle_reservation_confirmed(booking.pk), robust=True)
        return {"status": "confirmed"}
