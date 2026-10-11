"""Local receipt/allocation planning. Never charge a card or send a refund here."""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.notifications.models import AuditLog
from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingIntent, RefundObligation, Reservation

from .calculation import cents, refund_split
from .services import amount, enabled, launch_at, posting_transaction, restore_points

ZERO = Decimal("0.00")
PAID = [
    PaymentAttempt.Status.SUCCEEDED,
    PaymentAttempt.Status.PARTIALLY_REFUNDED,
    PaymentAttempt.Status.REFUNDED,
]


def eligible(booking):
    if not enabled() or not booking.booking_intent_id or booking.currency != "SAR":
        return False
    if (
        booking.source_type != Reservation.SourceType.DIRECT_WEBSITE
        or booking.created_at < launch_at()
    ):
        return False
    member = getattr(booking.booking_intent.customer, "member", None)
    return bool(member and member.status == "active" and member.consent_at <= timezone.now())


def payment_balances(booking):
    """Each actual receipt, including extension payments; reserve existing debts."""
    payments = list(
        PaymentAttempt.objects.filter(
            booking_intent_id=booking.booking_intent_id,
            currency="SAR",
            status__in=PAID,
            verified_at__isnull=False,
        )
        .exclude(provider_payment_id__isnull=True)
        .exclude(provider_payment_id="")
        .order_by("created_at", "pk")
    )
    used = {str(p.pk): ZERO for p in payments}
    by_provider = {p.provider_payment_id: str(p.pk) for p in payments}
    unassigned = ZERO
    for refund in booking.refund_obligations.exclude(status=RefundObligation.Status.CANCELLED):
        key = refund.calculation.get("payment_attempt") or by_provider.get(
            refund.calculation.get("hyperpay_refund", {}).get("original_payment_id")
        )
        if key in used:
            used[key] += refund.amount
        else:
            unassigned += refund.amount
    result = []
    for payment in payments:
        if payment.verified_at > timezone.now():
            raise ValidationError("وقت القبض غير صالح.")
        remaining = max(payment.amount - used[str(payment.pk)], ZERO)
        # Older unallocated obligations are conservatively reserved, not ignored.
        take = min(remaining, unassigned)
        remaining -= take
        unassigned -= take
        if payment.status == PaymentAttempt.Status.REFUNDED:
            remaining = ZERO
        result.append((payment, remaining))
    if unassigned:
        raise ValidationError("التزامات الاسترداد تتجاوز القبض الموثق.")
    return result


def funding(booking):
    cash = sum((value for _, value in payment_balances(booking)), ZERO)
    redemption = booking.loyalty_state.get("redemption", {})
    points = Decimal(redemption.get("value", "0")) - Decimal(redemption.get("restored", "0"))
    return cash, points


def cancellation_plan(booking):
    if not eligible(booking):
        return None
    cash, points = funding(booking)
    if booking.loyalty_state.get("arrival"):
        departure = booking.loyalty_state.get("departure_settlement")
        if not departure:
            raise ValidationError("يلزم توثيق المغادرة الفعلية قبل إلغاء إقامة بدأت.")
        restored = Decimal(booking.loyalty_state.get("redemption", {}).get("restored", "0"))
        target = Decimal(departure["points_refund"])
        pending_points = max(target - restored, ZERO)
        # The plan records the refundable cash across all original receipts.
        previous = sum(
            (
                r.amount
                for r in booking.refund_obligations.exclude(
                    status=RefundObligation.Status.CANCELLED
                )
            ),
            ZERO,
        )
        due_cash = max(Decimal(departure["cash_refund"]) - previous, ZERO)
        if due_cash > cash or pending_points > points:
            raise ValidationError("خطة المغادرة تتجاوز السداد المتبقي.")
        return {
            "cash": due_cash,
            "points": pending_points,
            "points_target": target,
            "source": "loyalty_actual_departure",
        }
    restored = Decimal(booking.loyalty_state.get("redemption", {}).get("restored", "0"))
    return {
        "cash": cash,
        "points": points,
        "points_target": restored + points,
        "source": "loyalty_full_prearrival",
    }


def decrease_plan(booking, final_total):
    if not eligible(booking):
        return None
    cash, points = funding(booking)
    difference = max(cash + points - Decimal(final_total), ZERO)
    split = refund_split(difference, cash, points)
    old_restored = Decimal(booking.loyalty_state.get("redemption", {}).get("restored", "0"))
    return {
        **split,
        "points_target": old_restored + split["points"],
        "source": "loyalty_proportional_decrease",
    }


def record_refund_plan(modification, plan, reason):
    """Paired point restoration and per-receipt debts after a confirmed change."""
    with posting_transaction():
        booking = Reservation.objects.select_for_update().get(pk=modification.reservation_id)
        if modification.status != modification.Status.COMPLETED:
            raise ValidationError("لا تسوية قبل تأكيد تعديل الحجز.")
        existing = list(
            booking.refund_obligations.filter(modification_request=modification).order_by(
                "created_at", "pk"
            )
        )
        if booking.loyalty_state.get("refund_plans", {}).get(str(modification.pk)):
            return existing
        if plan["points"]:
            restore_points(booking.pk, plan["points_target"], f"change:{modification.pk}")
            booking.refresh_from_db()
        remaining = plan["cash"]
        rows = []
        allocations = payment_balances(booking)
        if remaining > sum((value for _, value in allocations), ZERO):
            raise ValidationError("الاسترداد يتجاوز المقبوض.")
        for payment, ceiling in allocations:
            value = min(remaining, ceiling)
            if value <= 0:
                continue
            from apps.reservations.services.refunds import RefundComputation, record_obligation

            rows.append(
                record_obligation(
                    booking,
                    reason=reason,
                    modification=modification,
                    computation=RefundComputation(
                        value,
                        "SAR",
                        {
                            "source": plan["source"],
                            "payment_attempt": str(payment.pk),
                            "receipt_provider": payment.provider,
                            "points_target": str(plan["points_target"]),
                            "settlement_reference": str(modification.pk),
                        },
                    ),
                )
            )
            remaining -= value
        booking.loyalty_state.setdefault("refund_plans", {})[str(modification.pk)] = {
            **{key: str(value) for key, value in plan.items()},
            "obligations": [str(row.pk) for row in rows],
            "recorded_at": timezone.now().isoformat(),
        }
        booking.save(update_fields=["loyalty_state", "updated_at"])
        from .site_adapter import schedule_reconciliation

        schedule_reconciliation(booking.pk)
        return rows


def record_manual_receipt(
    operator, reservation_id, *, value, received_at, reference, lodging_only=False
):
    """Owner attestation of an already received payment; no provider request."""
    from apps.accounts.access import require_operations_owner

    require_operations_owner(operator)
    with posting_transaction():
        if lodging_only is not True:
            raise ValidationError("أكد أن السعر خاص بالإقامة فقط ولا يشمل رسومًا أخرى.")
        booking = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("booking_intent")
            .get(pk=reservation_id)
        )
        if (
            booking.source_type != Reservation.SourceType.DIRECT_WEBSITE
            or booking.created_at < launch_at()
            or booking.normalized_status not in Reservation.ACTIVE_STATUSES
        ):
            raise ValidationError("يلزم حجز موقع مستقبلي مؤكد.")
        if not booking.booking_intent_id or not hasattr(
            booking.booking_intent.quote, "manual_booking_draft"
        ):
            raise ValidationError("توثيق القبض مخصص لحجز أنشأته الإدارة.")
        n = amount(cents(value))
        if (
            n <= 0
            or booking.currency != "SAR"
            or not reference.strip()
            or timezone.is_naive(received_at)
            or received_at > timezone.now()
        ):
            raise ValidationError("يلزم مبلغ ومرجع ووقت قبض فعلي صحيح.")
        key = salted_hmac("loyalty-manual-receipt-v1", reference.strip()).hexdigest()
        prior = PaymentAttempt.objects.filter(idempotency_key=key).first()
        if prior:
            if (
                prior.booking_intent_id != booking.booking_intent_id
                or prior.amount != n
                or prior.verified_at != received_at
            ):
                raise ValidationError("المرجع مسجل ببيانات أخرى.")
            return prior
        if PaymentAttempt.objects.filter(
            booking_intent_id=booking.booking_intent_id,
            status__in=[
                PaymentAttempt.Status.CREATED,
                PaymentAttempt.Status.PENDING,
                PaymentAttempt.Status.UNKNOWN,
                PaymentAttempt.Status.REVIEW,
            ],
        ).exists():
            raise ValidationError("يلزم حسم عملية الدفع القائمة أولًا.")
        cash, points = funding(booking)
        if n > booking.total_price - cash - points:
            raise ValidationError("المبلغ يتجاوز المتبقي من الحجز.")
        row = PaymentAttempt.objects.create(
            booking_intent=booking.booking_intent,
            provider="manual",
            provider_payment_id=f"manual:{key}",
            provider_reference=reference.strip(),
            idempotency_key=key,
            amount=n,
            currency="SAR",
            status=PaymentAttempt.Status.SUCCEEDED,
            verified_at=received_at,
            payment_brand="manual_receipt",
        )
        AuditLog.objects.create(
            actor_user=operator,
            action="loyalty.manual_receipt",
            object_type="PaymentAttempt",
            object_reference=str(row.pk),
            summary="توثيق قبض فعلي لحجز إداري دون تحصيل جديد",
            metadata={
                "reservation": str(booking.pk),
                "amount": str(n),
                "received_at": received_at.isoformat(),
            },
        )
        booking.payment_status = (
            "paid" if cash + points + n == booking.total_price else "partially_paid"
        )
        booking.loyalty_state["manual_lodging_price"] = {
            "total": str(booking.total_price),
            "reference": reference.strip(),
            "operator": operator.pk,
            "at": timezone.now().isoformat(),
        }
        booking.save(update_fields=["payment_status", "loyalty_state", "updated_at"])
        if booking.payment_status == "paid":
            BookingIntent.objects.filter(pk=booking.booking_intent_id).update(
                status=BookingIntent.Status.COMPLETED
            )
        from .site_adapter import schedule_reconciliation

        schedule_reconciliation(booking.pk)
        return row


def priced_nights(availability):
    """Capture the offered calendar only if exact dates and contract total agree."""
    quote, calendar = availability.quote, availability.calendar_document
    if not quote or not calendar or quote.currency != "SAR":
        return {}
    rows = [day for day in calendar.days if quote.check_in <= day.date < quote.check_out]
    if len(rows) != quote.nights or any(
        day.price is None or day.price < 0 or day.price.quantize(Decimal("0.01")) != day.price
        for day in rows
    ):
        return {}
    dates = {quote.check_in + timedelta(days=n) for n in range(quote.nights)}
    if {day.date for day in rows} != dates or sum(
        (day.price for day in rows), ZERO
    ) != quote.total_price:
        return {}
    from apps.reservations.services.booking import sanitized_components

    from .site_adapter import _lodging

    if _lodging(sanitized_components(quote), quote.total_price) != quote.total_price:
        return {}
    return {
        "currency": quote.currency,
        "nights": [
            {"date": day.date.isoformat(), "value": str(day.price)}
            for day in sorted(rows, key=lambda day: day.date)
        ],
        "reference": f"offer:{quote.listing_id}:{quote.calculated_at.isoformat()}",
    }


def record_contract_nights(operator, reservation_id, *, nights, reference):
    """Explicit owner attestation when a discount prevents exact calendar capture."""
    from apps.accounts.access import require_operations_owner

    require_operations_owner(operator)
    with posting_transaction():
        booking = Reservation.objects.select_for_update().get(pk=reservation_id)
        if (
            booking.source_type != Reservation.SourceType.DIRECT_WEBSITE
            or booking.created_at < launch_at()
            or booking.currency != "SAR"
        ):
            raise ValidationError("حجز غير متاح لتوثيق الليالي.")
        if booking.loyalty_state.get("departure_settlement"):
            raise ValidationError("لا تُعدّل أسعار تسوية مغادرة موثقة.")
        if not reference.strip() or not isinstance(nights, list) or len(nights) != booking.nights:
            raise ValidationError("يلزم تفصيل أسعار ليالي العقد ومرجعها.")
        from datetime import date

        normalized = []
        for row in nights:
            day = date.fromisoformat(row["date"])
            normalized.append({"date": day.isoformat(), "value": str(amount(cents(row["value"])))})
        dates = [booking.check_in + timedelta(days=n) for n in range(booking.nights)]
        if (
            sorted(row["date"] for row in normalized) != [day.isoformat() for day in dates]
            or sum((Decimal(row["value"]) for row in normalized), ZERO) != booking.total_price
        ):
            raise ValidationError("الليالي أو مجموع أسعارها لا يطابق الحجز.")
        snapshot = {
            "currency": "SAR",
            "nights": sorted(normalized, key=lambda row: row["date"]),
            "reference": reference.strip(),
            "operator": operator.pk,
        }
        booking.loyalty_state["priced_nights"] = snapshot
        booking.save(update_fields=["loyalty_state", "updated_at"])
        AuditLog.objects.create(
            actor_user=operator,
            action="loyalty.contract_nights",
            object_type="Reservation",
            object_reference=str(booking.pk),
            summary="توثيق أسعار ليالي الحجز المتفق عليها",
            metadata=snapshot,
        )
        return snapshot
