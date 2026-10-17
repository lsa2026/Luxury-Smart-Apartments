"""Read-only presentation of an initial checkout's frozen cash/points split."""

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from apps.reservations.models import Reservation

COPY = {
    "ar": {
        "title": "تفاصيل سداد الحجز",
        "total": "إجمالي قيمة الحجز",
        "points": "المغطى بنقاط الولاء",
        "unit": "نقطة",
        "cash": "المبلغ المطلوب دفعه بالبطاقة",
        "paid": "المبلغ المدفوع بالبطاقة",
        "help": "النقاط تغطي جزءًا من قيمة الحجز ولا تغيّر إجماليه. كل نقطة تعادل ريالًا واحدًا.",
    },
    "en": {
        "title": "Booking payment breakdown",
        "total": "Total booking value",
        "points": "Covered by loyalty points",
        "unit": "points",
        "cash": "Amount to pay by card",
        "paid": "Amount paid by card",
        "help": (
            "Points cover part of the booking without changing its total. "
            "One point equals one Saudi riyal."
        ),
    },
    "fr": {
        "title": "Détail du règlement de la réservation",
        "total": "Valeur totale de la réservation",
        "points": "Couvert par les points de fidélité",
        "unit": "points",
        "cash": "Montant à régler par carte",
        "paid": "Montant réglé par carte",
        "help": (
            "Les points couvrent une partie de la réservation sans modifier son total. "
            "Un point vaut un riyal saoudien."
        ),
    },
}


def for_attempt(attempt):
    # Payment links and change requests have their own accounting boundaries.
    if attempt.modification_request_id or attempt.payment_request_id:
        return None
    intent = attempt.booking_intent
    if attempt.currency != "SAR":
        return None
    state = (
        Reservation.objects.filter(booking_intent=intent)
        .values_list("loyalty_state", flat=True)
        .first()
    ) or {}
    frozen = state.get("checkout_frozen") or {}
    if not isinstance(frozen, dict):
        return None
    try:
        points = Decimal(frozen.get("points", "0"))
        cash = Decimal(frozen.get("cash", "0"))
        from .valuation import intent_total_sar

        gross = intent_total_sar(intent)
        if (
            not points.is_finite()
            or not cash.is_finite()
            or not 0 < points <= gross
            or points.quantize(Decimal("0.01")) != points
            or cash != attempt.amount
            or cash + points != gross
            or intent.payment_amount_sar != gross
        ):
            return None
    except (InvalidOperation, ValueError, TypeError, ValidationError):
        return None
    text = COPY.get(intent.language, COPY["ar"])
    paid = bool(
        attempt.verified_at and attempt.status in {"succeeded", "partially_refunded", "refunded"}
    )
    return {
        "text": text,
        "gross": gross,
        "points": points,
        "cash": cash,
        "cash_label": text["paid" if paid else "cash"],
    }
