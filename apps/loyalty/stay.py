"""Trusted stay-ending facts; computes a plan, never submits a cash refund."""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.reservations.models import Reservation

from .calculation import cents, departure_refund, refund_split
from .launch import booking_allowed
from .services import _save_booking, posting_transaction


def record_departure(operator, reservation_id, *, vacated_at, reference):
    """Only a trusted booking-time priced-night snapshot can price early departure.

    Stage eight does not accept prices from a guest or fetch today's calendar as
    the historical booking price. Capturing such a snapshot in the quote flow
    remains a release prerequisite, not an assumed equal-nightly-price policy.
    """
    with posting_transaction():
        if (
            not operator.is_active
            or not operator.is_staff
            or not operator.has_perm("reservations.change_reservation")
        ):
            raise PermissionDenied("تأكيد المغادرة للإدارة المخولة فقط.")
        booking = Reservation.objects.select_related("property").get(pk=reservation_id)
        if not booking_allowed(booking):
            raise PermissionDenied("الحجز خارج نطاق التجربة الخاصة.")
        state = booking.loyalty_state
        arrived = state.get("arrival")
        original = state.get("settlement", {}).get("facts")
        priced = state.get("priced_nights")
        if not arrived or not original or not priced:
            raise ValidationError("يلزم وصول موثق وتفصيل أسعار الليالي المعتمد عند الحجز.")
        if timezone.is_naive(vacated_at) or vacated_at > timezone.now() or not reference:
            raise ValidationError("يلزم وقت مغادرة فعلي ومرجع موثق.")
        from datetime import datetime

        if vacated_at < datetime.fromisoformat(arrived["at"]):
            raise ValidationError("المغادرة لا تسبق الوصول.")
        nights = priced.get("nights", [])
        dates = [
            (booking.check_in + timedelta(days=n)).isoformat()
            for n in range((booking.check_out - booking.check_in).days)
        ]
        total = sum((Decimal(n["value"]) for n in nights), Decimal(0))
        if (
            [n["date"] for n in nights] != dates
            or total != Decimal(original["total"])
            or priced.get("currency") != "SAR"
            or not priced.get("reference")
            or booking.currency != "SAR"
        ):
            raise ValidationError("تفصيل الليالي لا يطابق الحجز المرجعي.")
        # This no-fee implementation deliberately does not invent an allocation
        # across tax/fee lines. The current user policy says no booking fees.
        components = original["components"]
        if any(p["type"] != "baseRate" and Decimal(p["total"]) != 0 for p in components):
            raise ValidationError("توزيع استرداد المكونات الإضافية يحتاج مراجعة.")
        funded_cash = total - Decimal(original["points"])
        if (
            funded_cash < 0
            or Decimal(original["cash"]) - Decimal(original["refunded"]) < funded_cash
        ):
            raise ValidationError("توزيع الدفع الأصلي غير مكتمل.")
        refundable = departure_refund(nights, vacated_at, booking.property.time_zone_name)
        split = refund_split(refundable, funded_cash, original["points"])
        result = {
            "at": vacated_at.isoformat(),
            "reference": reference,
            "operator": operator.pk,
            "priced_nights_reference": priced["reference"],
            "cash_refund": str(split["cash"]),
            "points_refund": str(split["points"]),
            "consumed_cash_lodging": str(funded_cash - split["cash"]),
            "refundable_total": str(refundable),
        }
        cents(result["consumed_cash_lodging"])
        old = state.get("departure_settlement")
        if old and old != result:
            raise ValidationError("المغادرة موثقة بالفعل ولا تستبدل بصمت.")
        state["departure_settlement"] = result
        _save_booking(booking)
        return result
