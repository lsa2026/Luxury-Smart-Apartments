"""Reviewed internal changes, with no Hostaway write or automatic refund."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.properties.models import Property

from .models import MorningStay
from .morning_stays import UNRESOLVED, MorningStayError, active_stays, property_zone, verify_window


def change_paid_stay(stay_id, values, *, expected_updated_at, client=None):
    current = MorningStay.objects.get(pk=stay_id)
    new_property = values["property"]
    window_changed = any(
        [
            current.property_id != new_property.pk,
            current.starts_at != values["starts_at"],
            current.ends_at != values["ends_at"],
            current.guests != values["guests"],
        ]
    )
    evidence = None
    if window_changed:
        evidence = verify_window(
            new_property,
            values["starts_at"],
            values["ends_at"],
            guests=values["guests"],
            client=client,
            allow_started=values["starts_at"] == current.starts_at,
        )
    with transaction.atomic():
        # Keep the same lock order as booking/payment allocation.
        list(
            Property.objects.select_for_update()
            .filter(pk__in=sorted({current.property_id, new_property.pk}))
            .order_by("pk")
        )
        stay = MorningStay.objects.select_for_update().get(pk=stay_id)
        if stay.updated_at.isoformat() != expected_updated_at:
            raise MorningStayError("تغير الحجز أثناء المراجعة؛ افتح التفاصيل مجددًا.")
        if stay.status != "paid" or stay.ends_at <= timezone.now():
            raise MorningStayError("التعديل متاح للحجز المدفوع والمؤكد قبل انتهاء الإقامة.")
        if (
            window_changed
            and active_stays(
                new_property,
                values["starts_at"],
                values["ends_at"] + timedelta(hours=3),
                exclude=stay.pk,
            ).exists()
        ):
            raise MorningStayError("توجد إقامة صباحية أخرى متداخلة؛ لم يُحفظ التعديل.")
        for field in (
            "property",
            "starts_at",
            "ends_at",
            "guest_name",
            "guest_phone",
            "guest_email",
            "guest_language",
            "guests",
        ):
            setattr(stay, field, values[field])
        stay.time_zone = property_zone(new_property).key
        if evidence:
            stay.availability_evidence = evidence
            stay.availability_checked_at = timezone.now()
        # Financial amount/attempts remain the verified payment's immutable facts.
        stay.save()
        return stay


def cancel_stay(stay_id):
    current = MorningStay.objects.get(pk=stay_id)
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=current.property_id)
        stay = MorningStay.objects.select_for_update().get(pk=stay_id)
        if stay.status == "cancelled":
            return stay
        if (
            stay.status in {"processing", "review"}
            or stay.attempts.filter(status__in=UNRESOLVED).exists()
        ):
            raise MorningStayError("توجد محاولة دفع غير محسومة؛ يلزم حسمها قبل الإلغاء.")
        stay.status = "cancelled"
        stay.save(update_fields=["status", "updated_at"])
        return stay
