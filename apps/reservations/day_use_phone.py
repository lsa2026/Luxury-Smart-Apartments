"""Scoped, audited phone correction with no automatic financial replay."""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient

from .day_use_hostaway import PROVIDER, _locked, _write_state, booking_state
from .day_use_stays import UNRESOLVED, DayUseStayError


def correct_phone(stay_id, phone, *, expected_updated_at, client=None):
    if not settings.DAY_USE08_ENABLED:
        raise DayUseStayError("المسار النهاري غير مفعّل.")
    if client is None:
        with HostawayClient(max_get_attempts=1, timeout=10) as remote:
            return correct_phone(
                stay_id, phone, expected_updated_at=expected_updated_at, client=remote
            )
    with transaction.atomic():
        stay = _locked(stay_id)
        operation = booking_state(stay)
        if (
            stay.updated_at.isoformat() != expected_updated_at
            or operation.get("state") != "confirmed"
            or stay.status not in {"open", "expired"}
            or stay.ends_at <= timezone.now()
            or stay.attempts.filter(status__in=(*UNRESOLVED, "succeeded")).exists()
            or stay.payment_events.exists()
            or stay.payment_link_deliveries.filter(
                state__in=["sending", "accepted", "uncertain", "delivered", "read"]
            ).exists()
        ):
            raise DayUseStayError(
                "تحتاج حالة الحجز أو الدفع أو الإرسال مراجعة قبل تصحيح الجوال؛ لم نحفظ تغييرًا."
            )
        if stay.guest_phone == phone:
            return stay
        _write_state(stay, operation | {"state": "phone_updating"})
    try:
        client.update_day_use_phone(
            operation["reservation_id"],
            phone=phone,
            listing_map_id=stay.property.hostaway_listing_map_id,
            arrival=stay.inventory_arrival_date,
            departure=stay.inventory_departure_date,
            amount=stay.amount_sar,
            source=PROVIDER,
        )
    except Exception as exc:
        with transaction.atomic():
            current = _locked(stay_id)
            _write_state(current, operation | {"state": "phone_unknown"})
        raise DayUseStayError("نتيجة تصحيح هوستاوي غير محسومة؛ راجعها قبل أي إعادة.") from exc
    with transaction.atomic():
        current = _locked(stay_id)
        if booking_state(current).get("state") != "phone_updating":
            raise DayUseStayError("تغيرت حالة التصحيح؛ يلزم مراجعة الإدارة.")
        _write_state(current, operation)
        current.guest_phone = phone
        current.save(update_fields=["guest_phone", "updated_at"])
        return current
