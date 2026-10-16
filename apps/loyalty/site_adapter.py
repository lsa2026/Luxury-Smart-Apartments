"""Read authoritative site records. Ambiguous allocation is a review, never a guess."""

import logging
from datetime import datetime, time
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.payments.currency import (
    CurrencyError,
    rates_from_audit_snapshot,
    validate_payment_snapshot,
)
from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingModificationRequest, RefundObligation, Reservation

logger = logging.getLogger(__name__)
CENT = Decimal("0.01")


def stay_times(booking):
    if not booking.property or not booking.property.time_zone_name:
        raise ValidationError("منطقة العقار الزمنية غير موثقة.")
    zone = ZoneInfo(booking.property.time_zone_name)
    return (
        datetime.combine(booking.check_in, time(15), zone),
        datetime.combine(booking.check_out, time(12), zone),
    )


def _lodging(components, total):
    if not isinstance(components, list) or not components:
        raise ValueError("price_breakdown_missing")
    sums, lodging, found = Decimal(0), Decimal(0), False
    for part in components:
        if not isinstance(part, dict) or part.get("included_in_total") is not True:
            raise ValueError("price_breakdown_unverified")
        value = Decimal(str(part["total"]))
        if not value.is_finite():
            raise ValueError("price_breakdown_invalid")
        sums += value
        # Discounts must already be included in baseRate. Unknown/negative
        # discount lines are not silently assigned to lodging versus fees.
        if value < 0:
            raise ValueError("discount_allocation_required")
        if part.get("type") == "baseRate":
            lodging += value
            found = True
    if sums != total or not found or not 0 <= lodging <= total:
        raise ValueError("price_breakdown_mismatch")
    return lodging


def booking_facts(booking):
    from .launch import booking_allowed, historical_award_allowed, stay_exception_allowed
    from .services import launch_at

    intent = booking.booking_intent
    if not booking_allowed(booking):
        return None, "outside_pilot_scope"
    historical_acceptance = historical_award_allowed(booking)
    test_stay_exception = stay_exception_allowed(booking)
    if (
        booking.source_type != Reservation.SourceType.DIRECT_WEBSITE
        or not intent
        or not intent.customer_id
        or (booking.created_at < launch_at() and not historical_acceptance)
    ):
        return None, "outside_program_scope"
    member = getattr(intent.customer, "member", None)
    if not member or member.status != "active":
        return None, "membership_required"
    arrival = booking.loyalty_state.get("arrival")
    try:
        from .accrual import scheduled_eligibility

        scheduled = scheduled_eligibility(booking) if not arrival else None
        if not arrival and not scheduled and not test_stay_exception:
            return None, "arrival_required"
        start, end = stay_times(booking)
        arrival_at = datetime.fromisoformat(arrival["at"]) if arrival else None
        if (
            (arrival_at is not None and timezone.is_naive(arrival_at))
            or (arrival_at is not None and arrival_at > timezone.now())
            or member.consent_at > timezone.now()
            or (member.consent_at > end and not historical_acceptance)
        ):
            return None, "stay_or_consent_not_eligible"
        payments = list(
            PaymentAttempt.objects.filter(
                booking_intent=intent,
                verified_at__isnull=False,
                status__in=[
                    PaymentAttempt.Status.SUCCEEDED,
                    PaymentAttempt.Status.PARTIALLY_REFUNDED,
                    PaymentAttempt.Status.REFUNDED,
                ],
            ).order_by("created_at", "pk")
        )
        if any(
            p.verified_at > timezone.now() or p.currency != "SAR" or not p.provider_payment_id
            for p in payments
        ):
            return None, "payment_receipt_reconciliation_required"
        refunds = list(
            booking.refund_obligations.filter(
                status=RefundObligation.Status.TRANSFERRED,
            ).order_by("pk")
        )
        if any(r.currency != "SAR" or not r.transferred_at for r in refunds):
            return None, "refund_currency_reconciliation_required"
        cash = sum((p.amount for p in payments), Decimal(0))
        returned = sum((r.amount for r in refunds), Decimal(0))
        if returned > cash:
            return None, "refund_exceeds_verified_cash"
        if any(p.status == PaymentAttempt.Status.REFUNDED for p in payments) and not refunds:
            return None, "refund_receipt_missing"
        redeemed = booking.loyalty_state.get("redemption", {})
        points = Decimal(redeemed.get("value", "0"))
        points -= Decimal(redeemed.get("restored", "0"))
        cancelled = booking.normalized_status == Reservation.Status.CANCELLED
        if not cancelled and booking.normalized_status not in Reservation.ACTIVE_STATUSES:
            return None, "booking_not_confirmed"
        components = intent.quote.components
        modification = (
            booking.modification_requests.filter(
                status=BookingModificationRequest.Status.COMPLETED,
            )
            .exclude(request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION)
            .order_by(
                "-completed_at",
                "-pk",
            )
            .first()
        )
        if modification:
            if (
                modification.new_check_in != booking.check_in
                or modification.new_check_out != booking.check_out
                or modification.new_total != booking.total_price
            ):
                return None, "modification_snapshot_mismatch"
            components = modification.quote_snapshot.get("components")
        elif intent.total_price != booking.total_price or (
            booking.currency == "SAR"
            and hasattr(intent.quote, "manual_booking_draft")
            and any(payment.provider == "manual" for payment in payments)
            and booking.loyalty_state.get("manual_lodging_price", {}).get("total")
            == str(booking.total_price)
        ):
            draft = getattr(intent.quote, "manual_booking_draft", None)
            if not draft or draft.final_total_price != booking.total_price:
                return None, "manual_price_breakdown_required"
            # An explicit lodging-only receipt attestation is required for
            # owner overrides. Do not infer fee allocation from a paid flag.
            receipt_facts = booking.loyalty_state.get("manual_lodging_price", {})
            if receipt_facts.get("total") != str(booking.total_price):
                return None, "manual_price_breakdown_required"
            components = [
                {"type": "baseRate", "total": str(booking.total_price), "included_in_total": True}
            ]
        legacy_trial_price = False
        if cancelled:
            # Cancellation is not proof that no nights were consumed.
            departure = booking.loyalty_state.get("departure_settlement")
            if arrival and not departure:
                return None, "departure_refund_evidence_required"
            previous = booking.loyalty_state.get("settlement", {}).get("facts")
            if not previous:
                return None, "cancelled_without_accrual"
            gross = Decimal(previous["total"])
            base = Decimal(departure["consumed_cash_lodging"]) if departure else Decimal(0)
        else:
            pricing_components = components
            # The single selected owner-test booking predates baseRate naming.
            # Only an explicit one-line accommodation quote is adapted in memory;
            # no broad legacy fallback, fee allocation, or source rewrite.
            legacy_trial_price = bool(
                test_stay_exception
                and not modification
                and isinstance(components, list)
                and len(components) == 1
                and isinstance(components[0], dict)
                and components[0].get("type") == "accommodation"
            )
            if legacy_trial_price:
                pricing_components = [{**components[0], "type": "baseRate"}]
            lodging = _lodging(pricing_components, booking.total_price)
            gross = booking.total_price
            if booking.currency != "SAR":
                if modification or points or refunds:
                    return None, "modified_fx_allocation_required"
                validate_payment_snapshot(
                    source_amount=intent.total_price,
                    source_currency=intent.currency,
                    payment_amount_sar=intent.payment_amount_sar,
                    snapshot=intent.exchange_rate_snapshot,
                )
                rate = rates_from_audit_snapshot(intent.exchange_rate_snapshot).rate(
                    booking.currency
                )
                gross = intent.payment_amount_sar
                lodging = (lodging / rate).quantize(CENT, rounding=ROUND_DOWN)
            if gross.quantize(CENT) != gross:
                return None, "booking_rounding_allocation_required"
            if cash - returned + points < gross:
                return None, "verified_payment_incomplete"
            if points and lodging != gross:
                return None, "mixed_fee_allocation_required"
            # Full-cash: lodging fully funded, fees excluded. No-fee mixed
            # booking: subtraction is exact, not a fee allocation policy.
            base = min(max(lodging - points, Decimal(0)), cash - returned).quantize(
                CENT,
                rounding=ROUND_DOWN,
            )
        if cash == 0 and points == 0 and not cancelled:
            return None, "verified_payment_required"
        facts = {
            "base": str(base),
            "total": str(gross),
            "cash": str(cash.quantize(CENT)),
            "refunded": str(returned.quantize(CENT)),
            "points": str(points.quantize(CENT)),
            "check_in": start.isoformat(),
            "check_out": end.isoformat(),
            "arrival": arrival
            or {
                "at": None,
                "reference": scheduled["reference"]
                if scheduled
                else "explicit-owner-test-stay-exception",
            },
            "consent": member.consent_at.isoformat(),
            "terms_version": member.terms_version,
            "cancelled": cancelled,
            "payments": [
                {
                    "id": str(p.pk),
                    "provider_id": p.provider_payment_id,
                    "at": p.verified_at.isoformat(),
                    "amount": str(p.amount),
                }
                for p in payments
            ],
            "refunds": [str(r.pk) for r in refunds],
            "modification": str(modification.pk) if modification else None,
            "components": components,
            "fx": intent.exchange_rate_snapshot,
            "rules": "verified-site-records-v1",
        }
        if historical_acceptance:
            facts["acceptance_scope"] = "explicit-historical-live-acceptance"
        if scheduled:
            facts["accrual_eligibility"] = scheduled
        if test_stay_exception and not arrival and not scheduled:
            facts["stay_exception"] = "explicit-owner-test-only"
        if legacy_trial_price:
            facts["price_adapter"] = "single-accommodation-owner-test-v1"
        return facts, ""
    except (ValueError, KeyError, InvalidOperation, CurrencyError) as exc:
        return None, str(exc) if isinstance(exc, ValueError) else "financial_facts_invalid"


def schedule_reconciliation(reservation_id):
    """Re-evaluate after a domain event, in addition to the due-stay scheduler."""
    from .services import enabled

    if enabled():
        transaction.on_commit(lambda: _safe_reconcile(reservation_id), robust=True)


def _safe_reconcile(reservation_id):
    from .launch import booking_allowed, pilot_only
    from .services import reconcile

    if pilot_only():
        booking = Reservation.objects.select_related("booking_intent__customer").get(
            pk=reservation_id
        )
        if not booking_allowed(booking):
            return

    try:
        from .checkout import restore_cancelled_points, settle_booking_points

        settle_booking_points(reservation_id)
        restore_cancelled_points(reservation_id)
        return reconcile(reservation_id)
    except Exception as exc:
        # An optional reward failure must not undo a confirmed paid reservation.
        # Persist only a safe error code, never keys or guest data. The existing
        # reservation remains the source for an explicit reconciliation retry.
        logger.exception("Loyalty reconciliation requires review: reservation=%s", reservation_id)
        with transaction.atomic():
            booking = Reservation.objects.select_for_update().get(pk=reservation_id)
            booking.loyalty_state["review"] = {
                "reason": "loyalty_processing_error",
                "exception_type": type(exc).__name__,
                "at": timezone.now().isoformat(),
            }
            booking.save(update_fields=["loyalty_state", "updated_at"])
        return {"status": "loyalty_processing_error", "movements": 0}
