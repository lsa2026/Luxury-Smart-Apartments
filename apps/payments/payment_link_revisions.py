"""Owner-reviewed balances for an existing stay, without rewriting old receipts."""

import hashlib
import json
from copy import copy
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.payments.currency import CurrencyService, validate_payment_snapshot
from apps.reservations.models import BookingModificationRequest, Reservation
from apps.reservations.services.automatic_modifications import execute_automatic_modification
from apps.reservations.services.owner_settlement import (
    owner_change_blocker,
    settlement_blocker,
    settlement_for,
)

from .hyperpay.exceptions import HyperPayCheckoutError
from .models import PaymentAttempt, ReservationPaymentRequest


def _digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def financial_signature(reservation, *, request_id=None):
    payments = PaymentAttempt.objects.filter(booking_intent_id=reservation.booking_intent_id)
    if request_id is not None:
        payments = payments.exclude(payment_request_id=request_id)
    receipt = getattr(reservation, "offline_payment_receipt", None)
    return _digest(
        {
            "payments": list(
                payments.order_by("pk").values_list(
                    "pk", "amount", "currency", "status", "verified_at", "provider_payment_id"
                )
            ),
            "receipt": (
                [receipt.pk, receipt.amount, receipt.currency, receipt.payment_attempt_id]
                if receipt
                else None
            ),
            "refunds": list(
                reservation.refund_obligations.order_by("pk").values_list(
                    "pk", "amount", "currency", "status"
                )
            ),
            "redemption": reservation.loyalty_state.get("redemption", {}),
        }
    )


def modification_signature(modification):
    return _digest(
        [
            modification.pk,
            modification.new_check_in,
            modification.new_check_out,
            modification.new_guests,
            modification.new_total,
            modification.currency,
            modification.quote_snapshot.get("owner_final_total"),
        ]
    )


def projected_reservation(modification):
    reservation = copy(modification.reservation)
    reservation.check_in = modification.new_check_in
    reservation.check_out = modification.new_check_out
    reservation.guests = modification.new_guests
    reservation.total_price = modification.new_total
    return reservation


def _pending(reservation):
    from .payment_links import UNRESOLVED

    return PaymentAttempt.objects.filter(
        booking_intent_id=reservation.booking_intent_id, status__in=UNRESOLVED
    ).exists()


def _cancellation_pending(reservation):
    return reservation.modification_requests.filter(
        request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION,
        status__in=["pending_admin_approval", "ready_for_hostaway", "processing", "unknown"],
    ).exists()


def _reviewable(modification):
    from .payment_links import require_enabled

    require_enabled()
    reservation = modification.reservation
    blocker = owner_change_blocker(modification, completed=True) or settlement_blocker(reservation)
    if blocker:
        raise HyperPayCheckoutError(blocker)
    if (
        not reservation.booking_intent_id
        or not reservation.hostaway_reservation_id
        or reservation.source_type != Reservation.SourceType.DIRECT_WEBSITE
        or (settings.HYPERPAY_ENVIRONMENT == "test") != reservation.is_test
    ):
        raise HyperPayCheckoutError("confirmed_direct_reservation_required")
    if (
        _pending(reservation)
        or _cancellation_pending(reservation)
        or reservation.payment_requests.filter(status="review").exists()
    ):
        raise HyperPayCheckoutError("previous_payment_unresolved")


def prepare_preview(reservation_id, modification_id):
    """Persist an inactive review only. No Hostaway write, gateway call or message."""
    from .payment_links import fingerprint

    with transaction.atomic():
        reservation = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("booking_intent", "property")
            .get(pk=reservation_id)
        )
        modification = BookingModificationRequest.objects.select_for_update().get(
            pk=modification_id, reservation=reservation
        )
        modification.reservation = reservation
        _reviewable(modification)
        balance = settlement_for(reservation, modification.new_total)
        if balance.due <= 0:
            raise HyperPayCheckoutError("nothing_due")
        expires_at = timezone.now() + timedelta(
            seconds=settings.BOOKING_MODIFICATION_REQUEST_TTL_SECONDS
        )
        with CurrencyService() as service:
            fx = service.create_quote(
                source_amount=balance.due,
                source_currency=modification.currency,
                display_currency=reservation.booking_intent.selected_display_currency or "SAR",
                quote_created_at=timezone.now(),
                quote_expires_at=expires_at,
            )
        anchor = reservation.payment_requests.exclude(status="draft").order_by("created_at").first()
        if anchor and anchor.link_anchor_id:
            anchor = anchor.link_anchor
        snapshot = {
            "version": 1,
            "total": str(modification.new_total),
            "paid": str(balance.paid),
            "due": str(balance.due),
            "financial_signature": financial_signature(reservation),
            "modification_signature": modification_signature(modification),
            "baseline": fingerprint(reservation),
            "fx": dict(fx.snapshot),
        }
        # A fresh review supersedes inactive reviews only. Active URLs are
        # untouched until the owner explicitly approves the selected draft.
        reservation.payment_requests.filter(status="draft").update(status="revoked")
        return ReservationPaymentRequest.objects.create(
            reservation=reservation,
            modification_request=modification,
            link_anchor=anchor,
            status="draft",
            amount_sar=fx.payment_amount_sar,
            source_amount=balance.due,
            source_currency=modification.currency,
            language=reservation.booking_intent.language,
            environment=settings.HYPERPAY_ENVIRONMENT,
            fingerprint=fingerprint(projected_reservation(modification)),
            settlement_snapshot=snapshot,
            expires_at=expires_at,
        )


def _check_preview(draft, reservation, modification):
    from .payment_links import fingerprint

    _reviewable(modification)
    snapshot = draft.settlement_snapshot
    balance = settlement_for(reservation, modification.new_total)
    if (
        draft.status != "draft"
        or draft.environment != settings.HYPERPAY_ENVIRONMENT
        or draft.expires_at is None
        or draft.expires_at <= timezone.now()
        or snapshot.get("modification_signature") != modification_signature(modification)
        or snapshot.get("financial_signature") != financial_signature(reservation)
        or Decimal(snapshot["paid"]) != balance.paid
        or draft.source_amount != balance.due
        or draft.source_currency != modification.currency
        or draft.fingerprint != fingerprint(projected_reservation(modification))
    ):
        raise HyperPayCheckoutError("payment_preview_changed")
    if modification.status != BookingModificationRequest.Status.COMPLETED and (
        snapshot.get("baseline") != fingerprint(reservation)
    ):
        raise HyperPayCheckoutError("payment_preview_changed")
    validate_payment_snapshot(
        source_amount=draft.source_amount,
        source_currency=draft.source_currency,
        payment_amount_sar=draft.amount_sar,
        snapshot=snapshot["fx"],
    )


def approve_preview(draft_id, *, modification_service=None, hostaway=None):
    """Owner approval: confirm the stay first, then atomically switch URL targets."""
    from .payment_links import fingerprint

    with transaction.atomic():
        hint = ReservationPaymentRequest.objects.get(pk=draft_id)
        reservation = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("booking_intent", "property")
            .get(pk=hint.reservation_id)
        )
        draft = ReservationPaymentRequest.objects.select_for_update().get(pk=draft_id)
        modification = BookingModificationRequest.objects.select_for_update().get(
            pk=draft.modification_request_id, reservation=reservation
        )
        modification.reservation = reservation
        if draft.approved_at is not None:
            validate_revision(draft, reservation)
            return draft
        _check_preview(draft, reservation, modification)

    if modification.status != BookingModificationRequest.Status.COMPLETED:
        outcome = execute_automatic_modification(
            modification, owner_override=True, service=modification_service
        )
        if outcome.code not in {"completed", "already_completed"}:
            raise HyperPayCheckoutError(outcome.code)

    reservation = Reservation.objects.select_related("booking_intent", "property").get(
        pk=reservation.pk
    )
    verify_revision_remote(draft, reservation, hostaway)

    # Recheck after the remote boundary. If funds changed during the update,
    # retain Hostaway's confirmed stay and require a new balance review.
    with transaction.atomic():
        reservation = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("booking_intent", "property")
            .get(pk=reservation.pk)
        )
        draft = ReservationPaymentRequest.objects.select_for_update().get(pk=draft_id)
        modification = BookingModificationRequest.objects.select_for_update().get(
            pk=draft.modification_request_id
        )
        modification.reservation = reservation
        if draft.approved_at is not None:
            validate_revision(draft, reservation)
            return draft
        _check_preview(draft, reservation, modification)
        # Verify the remote state even when a previous admin action completed
        # this modification. Approval itself never charges the guest.
        previous = reservation.payment_requests.exclude(pk=draft.pk).exclude(status="draft")
        previous.filter(status="open").update(status="revoked")
        draft.status = "open"
        draft.expires_at = None
        draft.approved_at = timezone.now()
        draft.fingerprint = fingerprint(reservation)
        draft.save(update_fields=["status", "expires_at", "approved_at", "fingerprint"])
        previous.update(superseded_by=draft)
        reservation.payment_status = (
            "partially_paid" if Decimal(draft.settlement_snapshot["paid"]) > 0 else "unpaid"
        )
        reservation.save(update_fields=["payment_status", "updated_at"])
        return draft


def validate_revision(request, reservation, *, completing=False):
    from .payment_links import fingerprint

    modification = request.modification_request
    modification.reservation = reservation
    snapshot = request.settlement_snapshot
    latest = (
        reservation.modification_requests.exclude(
            request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION
        )
        .order_by("-requested_at", "-pk")
        .first()
    )
    if (
        request.approved_at is None
        or request.status not in ({"open", "review"} if completing else {"open"})
        or request.environment != settings.HYPERPAY_ENVIRONMENT
        or modification.status != BookingModificationRequest.Status.COMPLETED
        or latest is None
        or latest.pk != modification.pk
        or reservation.normalized_status not in Reservation.ACTIVE_STATUSES
        or reservation.source_type != Reservation.SourceType.DIRECT_WEBSITE
        or _cancellation_pending(reservation)
        or reservation.payment_requests.filter(status="review").exclude(pk=request.pk).exists()
        or modification.new_check_in != reservation.check_in
        or modification.new_check_out != reservation.check_out
        or modification.new_guests != reservation.guests
        or modification.new_total != reservation.total_price
        or modification.quote_snapshot.get("owner_final_total") is None
        or request.fingerprint != fingerprint(reservation)
        or snapshot.get("modification_signature") != modification_signature(modification)
        or snapshot.get("financial_signature")
        != financial_signature(reservation, request_id=request.pk)
        or request.source_currency != reservation.currency
        or Decimal(snapshot["total"]) != reservation.total_price
        or Decimal(snapshot["paid"]) + request.source_amount != reservation.total_price
        or Decimal(snapshot["due"]) != request.source_amount
    ):
        raise HyperPayCheckoutError("approved_payment_balance_changed")
    validate_payment_snapshot(
        source_amount=request.source_amount,
        source_currency=request.source_currency,
        payment_amount_sar=request.amount_sar,
        snapshot=snapshot["fx"],
    )
    return reservation


def verify_revision_remote(request, reservation, client=None):
    from apps.integrations.hostaway.client import HostawayClient
    from apps.integrations.hostaway.reservation_validators import (
        normalize_hostaway_reservation_status,
    )

    def check(remote):
        snapshot = remote.get_reservation(reservation.hostaway_reservation_id)
        if (
            snapshot.reservation_id != reservation.hostaway_reservation_id
            or snapshot.listing_map_id != reservation.hostaway_listing_map_id
            or normalize_hostaway_reservation_status(snapshot.status)
            not in {"confirmed", "modified"}
            or snapshot.check_in != reservation.check_in
            or snapshot.check_out != reservation.check_out
            or snapshot.guests != reservation.guests
            or snapshot.currency != reservation.currency
            or snapshot.total_price != reservation.total_price
        ):
            raise HyperPayCheckoutError("hostaway_reservation_requires_review")
        paid = Decimal(request.settlement_snapshot["paid"])
        known = {}
        attempts = PaymentAttempt.objects.filter(
            booking_intent_id=reservation.booking_intent_id,
            status__in=["succeeded", "partially_refunded", "refunded"],
            verified_at__isnull=False,
        ).exclude(payment_request_id=request.pk)
        for attempt in attempts:
            if attempt.payment_request_id:
                value, currency = (
                    attempt.payment_request.source_amount,
                    attempt.payment_request.source_currency,
                )
            else:
                from .hostaway_ledger import _direct_receipt_value

                value, currency = _direct_receipt_value(attempt, reservation)
            known[f"LSA HyperPay receipt {attempt.pk}"] = (value, currency)
        observed = Decimal("0")
        markers = set()
        for charge in remote.get_guest_charges(reservation.hostaway_reservation_id):
            if str(charge.get("status", "")).casefold() != "paid":
                continue
            try:
                value = Decimal(str(charge.get("amount") or 0))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise HyperPayCheckoutError("unmatched_hostaway_collection") from exc
            if not value.is_finite() or value < 0:
                raise HyperPayCheckoutError("unmatched_hostaway_collection")
            if value <= 0:
                continue
            marker = charge.get("title")
            if marker in markers or known.get(marker) != (value, charge.get("currency")):
                raise HyperPayCheckoutError("unmatched_hostaway_collection")
            markers.add(marker)
            observed += value
        payment_status = snapshot.payment_status.strip().casefold()
        if (
            payment_status
            not in {"unpaid", "not_paid", "unknown", "", "paid", "fully_paid", "partially_paid"}
            or observed > paid
            or (
                payment_status in {"paid", "fully_paid", "partially_paid"}
                and (paid <= 0 or observed != paid)
            )
        ):
            raise HyperPayCheckoutError("unmatched_hostaway_collection")

    if client is not None:
        check(client)
    else:
        with HostawayClient() as remote:
            check(remote)
