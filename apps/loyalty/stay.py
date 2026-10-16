"""Trusted stay-ending facts; computes a plan, never submits a cash refund."""

from datetime import datetime, timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.reservations.models import Reservation

from .calculation import cents, departure_refund, refund_split
from .launch import booking_allowed
from .services import _save_booking, posting_transaction


def departure_plan(booking, *, vacated_at):
    """Read-only preview using payment facts and historical prices, never today's rates."""
    from .site_adapter import booking_facts, stay_times

    arrived = booking.loyalty_state.get("arrival")
    if not arrived:
        raise ValidationError("أكد وصول الضيف الفعلي أولًا من داخل الحجز.")
    if timezone.is_naive(vacated_at) or vacated_at > timezone.now():
        raise ValidationError("وقت المغادرة يجب أن يكون فعليًا، لا وقتًا مستقبليًا.")
    if vacated_at < datetime.fromisoformat(arrived["at"]):
        raise ValidationError("المغادرة لا تسبق الوصول.")
    original, blocker = booking_facts(booking)
    if blocker:
        raise ValidationError("يلزم حجز مؤهل وسداد إلكتروني موثق وعضوية نشطة لحساب التسوية.")
    total = Decimal(original["total"])
    if booking.currency != "SAR" or total != booking.total_price:
        raise ValidationError("يلزم تطابق قيمة الحجز بالريال قبل إنهاء الإقامة.")
    if any(p["type"] != "baseRate" and Decimal(p["total"]) != 0 for p in original["components"]):
        raise ValidationError("توزيع استرداد المكونات الإضافية يحتاج مراجعة.")
    funded_cash = total - Decimal(original["points"])
    if funded_cash < 0 or Decimal(original["cash"]) - Decimal(original["refunded"]) < funded_cash:
        raise ValidationError("توزيع الدفع الأصلي غير مكتمل.")
    _, end = stay_times(booking)
    normal = vacated_at >= end
    reference = "completed-contract"
    refundable = Decimal("0.00")
    if not normal:
        priced = booking.loyalty_state.get("priced_nights") or {}
        # Quotes already preserve this at checkout. Reuse it only for unchanged dates/total.
        if not priced and booking.booking_intent_id:
            quote = booking.booking_intent.quote
            if (quote.check_in, quote.check_out, quote.total_price) == (
                booking.check_in,
                booking.check_out,
                total,
            ):
                priced = quote.night_prices or {}
        # A single-night contract is exact; never invent an average for several nights.
        if not priced and booking.nights == 1:
            priced = {
                "currency": "SAR",
                "reference": "single-night-contract",
                "nights": [{"date": booking.check_in.isoformat(), "value": str(total)}],
            }
        nights = priced.get("nights", [])
        dates = [
            (booking.check_in + timedelta(days=n)).isoformat()
            for n in range((booking.check_out - booking.check_in).days)
        ]
        if (
            [n["date"] for n in nights] != dates
            or sum((Decimal(n["value"]) for n in nights), Decimal(0)) != total
            or priced.get("currency") != "SAR"
            or not priced.get("reference")
        ):
            raise ValidationError(
                "أسعار هذا الحجز القديم غير مكتملة؛ لم نحسب استردادًا تقديريًا أو نغيّر الحجز."
            )
        reference = priced["reference"]
        refundable = departure_refund(nights, vacated_at, booking.property.time_zone_name)
    split = refund_split(refundable, funded_cash, original["points"])
    return {
        "priced_nights_reference": reference,
        "cash_refund": format(split["cash"], ".2f"),
        "points_refund": format(split["points"], ".2f"),
        "consumed_cash_lodging": format(funded_cash - split["cash"], ".2f"),
        "refundable_total": format(refundable, ".2f"),
        "normal": normal,
    }


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
        booking = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("property", "booking_intent__quote", "booking_intent__customer")
            .get(pk=reservation_id)
        )
        if not booking_allowed(booking):
            raise PermissionDenied("الحجز خارج نطاق التجربة الخاصة.")
        state = booking.loyalty_state
        if not reference:
            raise ValidationError("يلزم مرجع موثق.")
        result = {
            **departure_plan(booking, vacated_at=vacated_at),
            "at": vacated_at.isoformat(),
            "reference": reference,
            "operator": operator.pk,
        }
        cents(result["consumed_cash_lodging"])
        old = state.get("departure_settlement")
        if old and old != result:
            raise ValidationError("المغادرة موثقة بالفعل ولا تستبدل بصمت.")
        state["departure_settlement"] = result
        _save_booking(booking)
        return result
