"""Read-only, bounded financial summaries for the private booking list.

Collections stay in their recorded currencies. A booking total or a provider's
payment-status label is not evidence of receipt, and a pending refund is not a
completed transfer. No gateway, exchange-rate, or channel-manager calls belong
on this presentation path.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.payments.models import OfflinePaymentReceipt, PaymentAttempt
from apps.reservations.models import RefundObligation, Reservation


@dataclass(frozen=True, slots=True)
class MoneyTotal:
    currency: str
    amount: Decimal


def attach_booking_list_summaries(bookings: list[Reservation]) -> None:
    """Attach display-only attributes with three bulk queries, never per row."""
    if not bookings:
        return
    moment = timezone.now()
    reservation_ids = [booking.pk for booking in bookings]
    intent_ids = [booking.booking_intent_id for booking in bookings if booking.booking_intent_id]
    paid = defaultdict(lambda: defaultdict(Decimal))
    refunded = defaultdict(lambda: defaultdict(Decimal))
    payments = (
        PaymentAttempt.objects.filter(
            booking_intent_id__in=intent_ids,
            provider__in=("hyperpay", "manual"),
            status__in=(
                PaymentAttempt.Status.SUCCEEDED,
                PaymentAttempt.Status.PARTIALLY_REFUNDED,
                PaymentAttempt.Status.REFUNDED,
            ),
            verified_at__lte=moment,
            provider_payment_id__isnull=False,
            amount__gt=0,
        )
        .exclude(provider_payment_id="")
        .exclude(provider_result_code="000.100.112")
        .order_by()
        .values("booking_intent_id", "currency")
        .annotate(total=Sum("amount"))
    )
    for row in payments:
        paid[row["booking_intent_id"]][row["currency"]] += row["total"]
    # A linked offline receipt already appears as a manual PaymentAttempt.
    receipts = (
        OfflinePaymentReceipt.objects.filter(
            reservation_id__in=reservation_ids,
            payment_attempt__isnull=True,
            received_at__lte=moment,
        )
        .order_by()
        .values("reservation_id", "currency")
        .annotate(total=Sum("amount"))
    )
    standalone = defaultdict(lambda: defaultdict(Decimal))
    for row in receipts:
        standalone[row["reservation_id"]][row["currency"]] += row["total"]
    refunds = (
        RefundObligation.objects.filter(
            reservation_id__in=reservation_ids,
            status=RefundObligation.Status.TRANSFERRED,
            transferred_at__lte=moment,
            amount__gt=0,
        )
        .order_by()
        .values("reservation_id", "currency")
        .annotate(total=Sum("amount"))
    )
    for row in refunds:
        refunded[row["reservation_id"]][row["currency"]] += row["total"]
    for booking in bookings:
        totals = dict(paid[booking.booking_intent_id])
        for currency, amount in standalone[booking.pk].items():
            totals[currency] = totals.get(currency, Decimal("0")) + amount
        booking.admin_paid_totals = tuple(
            MoneyTotal(currency, amount) for currency, amount in sorted(totals.items())
        )
        booking.admin_refunded_totals = tuple(
            MoneyTotal(currency, amount)
            for currency, amount in sorted(refunded[booking.pk].items())
        )
        booking.admin_payment_note = (
            "سداد بالنقاط"
            if booking.payment_status == "paid_with_points" and not totals
            else "لا يوجد مبلغ موثق محليًا"
            if not totals
            else ""
        )
        if booking.is_test:
            booking.admin_paid_totals = ()
            booking.admin_refunded_totals = ()
            booking.admin_payment_note = "حجز تجريبي"
        booking.admin_average_nightly_price = (
            booking.total_price / booking.nights if booking.nights else None
        )
