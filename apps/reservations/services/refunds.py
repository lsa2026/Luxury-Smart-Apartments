"""What the platform owes a guest, and the record that keeps that debt visible.

The money leaves through a bank transfer that a person makes; nothing here calls
a payment provider. What this module guarantees is that the amount is computed
from the administration's own policy table, written down the moment the booking
changes, deducted from reported income until it is settled, and announced to the
administration with the contact details needed to reach the guest.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db import DatabaseError
from django.utils import timezone

from ..models import CancellationPolicyTier, RefundObligation, Reservation
from .host_policy import check_in_datetime

logger = logging.getLogger(__name__)

CENTS = Decimal("0.0001")
_CLEANING_MARKERS = ("cleaning", "clean", "تنظيف")


@dataclass(frozen=True, slots=True)
class RefundComputation:
    """The amount owed and, just as importantly, how it was reached."""

    amount: Decimal
    currency: str
    detail: dict[str, Any] = field(default_factory=dict)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def _cleaning_fee(reservation: Reservation) -> Decimal:
    """The cleaning component of the stay, as Hostaway itemised it."""
    intent = reservation.booking_intent
    quote = getattr(intent, "quote", None) if intent else None
    components = getattr(quote, "components", None)
    if not isinstance(components, list):
        return Decimal("0")
    total = Decimal("0")
    for component in components:
        if not isinstance(component, dict):
            continue
        haystack = f"{component.get('type', '')} {component.get('title', '')}".casefold()
        if not any(marker in haystack for marker in _CLEANING_MARKERS):
            continue
        try:
            total += Decimal(str(component.get("total", "0")))
        except (ArithmeticError, TypeError, ValueError):
            continue
    return max(total, Decimal("0"))


def cancellation_refund(
    reservation: Reservation,
    *,
    at: datetime | None = None,
) -> RefundComputation:
    """Apply the administration's tier table for this property's policy.

    A policy with no configured tier refunds nothing: an empty table means the
    numbers have not been decided yet, which must never be read as "refund all".
    """
    currency = reservation.currency
    policy = ""
    if reservation.property is not None:
        policy = (reservation.property.cancellation_policy or "").strip()
    moment = at or timezone.now()
    detail: dict[str, Any] = {
        "policy_code": policy,
        "stay_total": format(reservation.total_price, "f"),
    }

    if not policy:
        detail["reason"] = "no_policy_on_property"
        return RefundComputation(Decimal("0"), currency, detail)

    arrival = check_in_datetime(reservation.property, reservation.check_in)
    hours_before = (arrival - moment).total_seconds() / 3600
    detail["hours_before_check_in"] = round(hours_before, 2)
    if hours_before < 0:
        hours_before = 0.0

    tier = (
        CancellationPolicyTier.objects.filter(
            policy_code__iexact=policy,
            is_active=True,
            min_hours_before_check_in__lte=int(hours_before),
        )
        .order_by("-min_hours_before_check_in")
        .first()
    )
    if tier is None:
        detail["reason"] = "no_matching_tier"
        return RefundComputation(Decimal("0"), currency, detail)

    percentage = Decimal(tier.refund_percentage)
    base = Decimal(reservation.total_price)
    cleaning = Decimal("0")
    if not tier.refunds_cleaning_fee:
        cleaning = min(_cleaning_fee(reservation), base)
        base -= cleaning
    amount = _quantize(base * percentage / Decimal("100"))
    detail.update(
        {
            "tier_id": tier.pk,
            "min_hours_before_check_in": tier.min_hours_before_check_in,
            "refund_percentage": format(percentage, "f"),
            "refunds_cleaning_fee": tier.refunds_cleaning_fee,
            "cleaning_fee_withheld": format(cleaning, "f"),
            "refundable_base": format(base, "f"),
        }
    )
    return RefundComputation(max(amount, Decimal("0")), currency, detail)


def record_obligation(
    reservation: Reservation,
    *,
    reason: str,
    computation: RefundComputation,
    modification: object | None = None,
) -> RefundObligation | None:
    """Write the debt down and tell the administration it exists.

    Returns None for a zero amount: a policy that returns nothing is a valid
    outcome and must not create an empty task for someone to close.
    """
    if computation.amount <= 0:
        return None
    obligation = RefundObligation.objects.create(
        reservation=reservation,
        modification_request=modification,
        reason=reason,
        amount=computation.amount,
        currency=computation.currency,
        calculation=computation.detail,
    )
    _announce(obligation)
    return obligation


def _announce(obligation: RefundObligation) -> None:
    """Never let a notification failure undo a settled booking change."""
    try:
        from apps.notifications.services.events import handle_refund_due

        handle_refund_due(obligation.pk)
    except (DatabaseError, ImportError, ValueError) as error:
        logger.error(
            "Refund obligation announced with no notification: reference=%s code=%s",
            obligation.public_reference,
            type(error).__name__,
        )


def outstanding_total(currency: str) -> Decimal:
    """What the platform still owes guests in one currency."""
    from django.db.models import Sum

    total = RefundObligation.objects.filter(
        status=RefundObligation.Status.DUE,
        currency=currency,
    ).aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0")
