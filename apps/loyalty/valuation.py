"""Loyalty is denominated in SAR; never fetch a rate to revalue an old stay."""

from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError

from apps.payments.currency import (
    CurrencyError,
    normalize_amount,
    validate_payment_snapshot,
)


def intent_total_sar(intent):
    try:
        if intent.currency == "SAR":
            if intent.payment_amount_sar != intent.total_price:
                raise ValidationError("قيمة التسوية بالريال لا تطابق الحجز.")
        else:
            validate_payment_snapshot(
                source_amount=intent.total_price,
                source_currency=intent.currency,
                payment_amount_sar=intent.payment_amount_sar,
                snapshot=intent.exchange_rate_snapshot,
            )
        return intent.payment_amount_sar
    except CurrencyError as exc:
        raise ValidationError("يلزم سعر صرف الحجز الأصلي الموثق قبل تسوية النقاط.") from exc


def to_sar(booking, value):
    if booking.currency == "SAR":
        return normalize_amount(value, "SAR")
    source, sar = _contract(booking)
    if not source:
        if Decimal(value):
            raise ValidationError("لا تحويل لقيمة غير صفرية عن عقد صفري.")
        return Decimal("0.00")
    return (Decimal(value) * sar / source).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def total_sar(booking):
    if booking.currency == "SAR":
        return normalize_amount(booking.total_price, "SAR")
    return _contract(booking)[1]


def to_source(booking, value):
    if booking.currency == "SAR":
        return Decimal(value)
    source, sar = _contract(booking)
    if not sar:
        if Decimal(value):
            raise ValidationError("لا تحويل لقيمة غير صفرية عن عقد صفري.")
        return Decimal("0.00")
    return normalize_amount(Decimal(value) * source / sar, booking.currency)


def _contract(booking):
    """Rebuild the SAR contract from each accepted funding snapshot in order.

    Increases keep their own approved rate; decreases retain the historical
    funded ratio. No provider lookup, invented rate, or relabelled receipt.
    """
    from apps.reservations.models import BookingModificationRequest

    intent = booking.booking_intent
    sar = intent_total_sar(intent)
    source = intent.total_price
    if booking.currency != intent.currency:
        raise ValidationError("عملة الحجز لا تطابق لقطة الصرف الأصلية.")
    changes = booking.modification_requests.filter(
        status=BookingModificationRequest.Status.COMPLETED,
    ).exclude(request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION)
    for change in changes.order_by("completed_at", "pk"):
        if (
            change.old_total != source
            or change.currency != booking.currency
            or change.new_total is None
        ):
            raise ValidationError("تسلسل تسويات الحجز بالعملات غير مكتمل.")
        target = change.new_total
        if target < 0:
            raise ValidationError("قيمة تسوية الحجز غير صحيحة.")
        if target > source:
            snapshot = change.quote_snapshot.get("fx")
            try:
                validate_payment_snapshot(
                    source_amount=target - source,
                    source_currency=booking.currency,
                    payment_amount_sar=change.payment_amount_sar,
                    snapshot=snapshot,
                )
            except CurrencyError as exc:
                raise ValidationError("يلزم توثيق تمويل زيادة الحجز وسعر صرفها.") from exc
            sar += change.payment_amount_sar
        elif source:
            sar = (sar * target / source).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        source = target
    if source != booking.total_price:
        raise ValidationError("إجمالي الحجز لا يطابق تسويات العملة الموثقة.")
    return source, sar
