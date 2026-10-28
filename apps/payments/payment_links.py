"""Collect for an existing stay; never quote, create inventory, or refund here."""

import hashlib
import json
import secrets
from decimal import Decimal
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.core.guest_documents import documents_digest
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.reservation_validators import normalize_hostaway_reservation_status
from apps.payments.currency import validate_payment_snapshot
from apps.reservations.models import BookingIntent, Reservation

from .hyperpay.exceptions import HyperPayCheckoutError, HyperPayError
from .hyperpay.service import (
    CheckoutSession,
    HyperPayService,
    build_checkout_payload,
    merchant_transaction_id,
)
from .models import PaymentAttempt, ReservationPaymentRequest

SALT = "payments.existing-reservation.v1"
SETTLED = ("succeeded", "partially_refunded", "refunded")
UNRESOLVED = ("created", "pending", "unknown", "review")


def require_enabled():
    if not settings.RESERVATION_PAYMENT_LINKS_ENABLED or not settings.HYPERPAY_ENABLED:
        raise HyperPayCheckoutError("payment_links_disabled")


def fingerprint(reservation):
    intent = reservation.booking_intent
    data = {
        "reservation": str(reservation.pk),
        "intent": str(intent.pk),
        "hostaway": reservation.hostaway_reservation_id,
        "listing": reservation.hostaway_listing_map_id,
        "property": str(reservation.property_id),
        "is_test": reservation.is_test,
        "arrival": str(reservation.check_in),
        "departure": str(reservation.check_out),
        "guests": reservation.guests,
        "total": format(reservation.total_price, ".4f"),
        "currency": reservation.currency,
        "amount": str(intent.payment_amount_sar),
        "fx": intent.exchange_rate_snapshot,
        "email": intent.guest_email,
        "phone": intent.guest_phone,
        "first": intent.guest_first_name,
        "last": intent.guest_last_name,
        "language": intent.language,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _eligible(reservation):
    intent = reservation.booking_intent
    if (
        not intent
        or not reservation.hostaway_reservation_id
        or reservation.normalized_status != Reservation.Status.CONFIRMED
        or reservation.source_type
        not in {Reservation.SourceType.DIRECT_WEBSITE, Reservation.SourceType.HOSTAWAY_MANUAL}
    ):
        raise HyperPayCheckoutError("confirmed_direct_reservation_required")
    if (settings.HYPERPAY_ENVIRONMENT == "test") != reservation.is_test:
        raise HyperPayCheckoutError("payment_environment_mismatch")
    if reservation.payment_status.casefold() in {"paid", "fully_paid", "partially_paid"}:
        raise HyperPayCheckoutError("existing_collection_requires_review")
    if hasattr(reservation, "offline_payment_receipt"):
        raise HyperPayCheckoutError("offline_collection_already_recorded")
    if (
        intent.total_price != reservation.total_price
        or intent.currency != reservation.currency
        or intent.property_id != reservation.property_id
        or intent.check_in != reservation.check_in
        or intent.check_out != reservation.check_out
        or intent.guests != reservation.guests
    ):
        raise HyperPayCheckoutError("reservation_snapshot_changed")
    validate_payment_snapshot(
        source_amount=intent.total_price,
        source_currency=intent.currency,
        payment_amount_sar=intent.payment_amount_sar,
        snapshot=intent.exchange_rate_snapshot,
    )
    if not intent.payment_amount_sar or intent.payment_amount_sar <= 0:
        raise HyperPayCheckoutError("nothing_due")
    if (
        reservation.refund_obligations.exists()
        or reservation.modification_requests.filter(
            status__in=[
                "requested",
                "pending_admin_approval",
                "ready_for_hostaway",
                "awaiting_payment",
                "processing",
                "completed",
            ]
        ).exists()
    ):
        raise HyperPayCheckoutError("booking_change_requires_review")
    return intent


def issue_request(reservation_id):
    require_enabled()
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=reservation_id)
        revised = reservation.payment_requests.filter(
            status="open", modification_request__isnull=False
        ).first()
        if revised is not None:
            validate_open(revised)
            return revised
        intent = _eligible(reservation)
        current = reservation.payment_requests.filter(status="open").first()
        if current:
            # The owner removed temporal expiry, including legacy deadlines.
            # A changed booking still cannot replace an unresolved gateway attempt.
            if current.fingerprint == fingerprint(reservation):
                return current
            if current.attempts.filter(status__in=UNRESOLVED).exists():
                raise HyperPayCheckoutError("previous_payment_unresolved")
            current.status = "revoked"
            current.save(update_fields=["status"])
        if PaymentAttempt.objects.filter(
            booking_intent=intent, status__in=(*SETTLED, *UNRESOLVED)
        ).exists():
            raise HyperPayCheckoutError("previous_collection_requires_review")
        return ReservationPaymentRequest.objects.create(
            reservation=reservation,
            amount_sar=intent.payment_amount_sar,
            source_amount=reservation.total_price,
            source_currency=reservation.currency,
            fingerprint=fingerprint(reservation),
            language=intent.language,
            environment=settings.HYPERPAY_ENVIRONMENT,
            expires_at=None,
        )


def link_token(request):
    # Deterministic across resends; validity and revocation are checked in the database.
    return signing.Signer(salt=SALT).sign_object(
        {"request": str(getattr(request, "link_anchor_id", None) or request.pk)}
    )


def public_url(request):
    base = settings.SITE_BASE_URL.rstrip("/")
    parsed = urlsplit(base)
    if not parsed.hostname or (not settings.DEBUG and parsed.scheme != "https"):
        raise ValueError("payment_link_origin_invalid")
    return base + reverse("payments:reservation_payment", args=[link_token(request)])


def resolve_token(token):
    try:
        payload = signing.Signer(salt=SALT).unsign_object(token)
        if not isinstance(payload, dict) or set(payload) != {"request"}:
            return None
        request = (
            ReservationPaymentRequest.objects.select_related(
                "reservation__booking_intent", "reservation__property"
            )
            .filter(pk=payload["request"])
            .first()
        )
        visited = set()
        while request and request.superseded_by_id:
            if request.pk in visited or len(visited) >= 20:
                return None
            visited.add(request.pk)
            next_request = request.superseded_by
            if (
                next_request.reservation_id != request.reservation_id
                or next_request.environment != request.environment
                or next_request.approved_at is None
            ):
                return None
            request = next_request
        return request
    except (signing.BadSignature, ValidationError, ValueError, TypeError, KeyError):
        return None


def validate_open(request):
    if request.status != "open" or request.environment != settings.HYPERPAY_ENVIRONMENT:
        raise HyperPayCheckoutError("payment_request_not_open")
    reservation = Reservation.objects.select_related("booking_intent").get(
        pk=request.reservation_id
    )
    if request.modification_request_id:
        from .payment_link_revisions import validate_revision

        return validate_revision(request, reservation)
    _eligible(reservation)
    if (
        request.fingerprint != fingerprint(reservation)
        or request.source_amount != reservation.total_price
        or request.source_currency != reservation.currency
        or request.amount_sar != reservation.booking_intent.payment_amount_sar
    ):
        raise HyperPayCheckoutError("reservation_snapshot_changed")
    return reservation


def _verify_remote(reservation, client=None):
    def check(c):
        snapshot = c.get_reservation(reservation.hostaway_reservation_id)
        if (
            snapshot.reservation_id != reservation.hostaway_reservation_id
            or snapshot.listing_map_id != reservation.hostaway_listing_map_id
            or normalize_hostaway_reservation_status(snapshot.status) != "confirmed"
            or snapshot.check_in != reservation.check_in
            or snapshot.check_out != reservation.check_out
            or snapshot.guests != reservation.guests
            or snapshot.currency != reservation.currency
            or snapshot.total_price != reservation.total_price
            or snapshot.payment_status.casefold() not in {"unpaid", "not_paid", "unknown", ""}
        ):
            raise HyperPayCheckoutError("hostaway_reservation_requires_review")
        charges = c.get_guest_charges(reservation.hostaway_reservation_id)
        if any(
            str(charge.get("status", "")).casefold() == "paid"
            and Decimal(str(charge.get("amount") or 0)) > 0
            for charge in charges
        ):
            raise HyperPayCheckoutError("existing_hostaway_collection_requires_review")

    if client is not None:
        check(client)
    else:
        with HostawayClient() as c:
            check(c)


def create_request_checkout(request_id, *, billing_data, acceptance, client=None, hostaway=None):
    require_enabled()
    if acceptance.get("documents_digest") != documents_digest() or acceptance.get("consents") != {
        "terms": True,
        "house_rules": True,
    }:
        raise HyperPayCheckoutError("guest_acceptance_required")
    request = ReservationPaymentRequest.objects.get(pk=request_id)
    reservation = validate_open(request)
    # Read an existing reservation, never an availability query or a booking POST.
    if request.modification_request_id:
        from .payment_link_revisions import verify_revision_remote

        verify_revision_remote(request, reservation, hostaway)
    else:
        _verify_remote(reservation, hostaway)
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
        request = ReservationPaymentRequest.objects.select_for_update().get(pk=request_id)
        validate_open(request)
        intent = BookingIntent.objects.select_for_update().get(pk=reservation.booking_intent_id)
        attempts = PaymentAttempt.objects.filter(booking_intent=intent)
        if request.modification_request_id:
            attempts = attempts.filter(
                Q(status__in=UNRESOLVED) | Q(payment_request=request, status__in=SETTLED)
            )
        else:
            attempts = attempts.filter(status__in=(*SETTLED, *UNRESOLVED))
        previous = attempts.order_by("-created_at").first()
        if previous:
            if (
                previous.payment_request_id == request.pk
                and previous.status == "pending"
                and previous.provider_checkout_id
                and previous.widget_integrity
            ):
                return CheckoutSession(
                    previous, previous.provider_checkout_id, previous.widget_integrity
                )
            raise HyperPayCheckoutError("previous_payment_unresolved")
        # Billing is entered by the guest, never fabricated from the owner's draft.
        for field in (
            "billing_street1",
            "billing_city",
            "billing_state",
            "billing_country",
            "billing_postcode",
        ):
            setattr(intent, field, billing_data[field])
        now = timezone.now()
        fields = [
            "billing_street1",
            "billing_city",
            "billing_state",
            "billing_country",
            "billing_postcode",
            "updated_at",
        ]
        if not request.modification_request_id:
            intent.terms_accepted_at = intent.privacy_accepted_at = now
            intent.house_rules_accepted_at = now
            intent.legal_acceptance = acceptance
            fields += [
                "terms_accepted_at",
                "privacy_accepted_at",
                "house_rules_accepted_at",
                "legal_acceptance",
            ]
        intent.save(update_fields=fields)
        request.accepted_at, request.legal_acceptance = now, acceptance
        request.save(update_fields=["accepted_at", "legal_acceptance"])
        merchant_id = merchant_transaction_id()
        payload = build_checkout_payload(intent, merchant_id, amount=request.amount_sar)
        attempt = PaymentAttempt.objects.create(
            booking_intent=intent,
            payment_request=request,
            provider="hyperpay",
            amount=request.amount_sar,
            currency="SAR",
            merchant_transaction_id=merchant_id,
            idempotency_key=secrets.token_urlsafe(32),
        )
    # Commit the unique claim BEFORE contacting the gateway. A timeout/crash is
    # unresolved, never permission to create a second potentially chargeable session.
    try:
        with HyperPayService(client=client) as service:
            response = service.client.create_checkout(payload)
            checkout_id, integrity, _ = service._validate_checkout_response(response, attempt)
    except Exception as exc:
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            status="review", failure_code="checkout_outcome_uncertain"
        )
        raise HyperPayCheckoutError("checkout_outcome_uncertain") from exc
    return CheckoutSession(attempt, checkout_id, integrity)


def complete_request_payment(attempt):
    """The financial success is durable even if inventory subsequently changed."""
    from apps.integrations.monitoring import alert_operations
    from apps.notifications.services.events import handle_reservation_confirmed
    from apps.payments.hostaway_ledger import queue_payment_receipt

    from .payment_link_tasks import queue_delivery

    with transaction.atomic():
        request = ReservationPaymentRequest.objects.get(pk=attempt.payment_request_id)
        reservation = Reservation.objects.select_for_update().get(pk=request.reservation_id)
        request = ReservationPaymentRequest.objects.select_for_update().get(pk=request.pk)
        if request.status == "paid":
            return reservation
        revision_valid = False
        if request.modification_request_id:
            from .payment_link_revisions import validate_revision

            try:
                validate_revision(request, reservation, completing=True)
                revision_valid = (
                    attempt.amount == request.amount_sar
                    and attempt.currency == "SAR"
                    and attempt.booking_intent_id == reservation.booking_intent_id
                    and attempt.status == "succeeded"
                    and attempt.verified_at is not None
                    and not request.attempts.filter(status__in=SETTLED)
                    .exclude(pk=attempt.pk)
                    .exists()
                )
            except (HyperPayError, ValueError, ValidationError):
                pass
        legacy_invalid = (
            reservation.normalized_status != Reservation.Status.CONFIRMED
            or request.fingerprint != fingerprint(reservation)
            or request.status not in {"open", "review"}
            or attempt.amount != request.amount_sar
            or attempt.currency != "SAR"
            or attempt.booking_intent_id != reservation.booking_intent_id
            or request.environment != settings.HYPERPAY_ENVIRONMENT
            or request.source_amount != reservation.total_price
            or request.source_currency != reservation.currency
            or request.amount_sar != reservation.booking_intent.payment_amount_sar
            or reservation.payment_status.casefold() == "paid"
            or PaymentAttempt.objects.filter(
                booking_intent_id=attempt.booking_intent_id, status__in=SETTLED
            )
            .exclude(pk=attempt.pk)
            .exists()
        )
        if (request.modification_request_id and not revision_valid) or (
            not request.modification_request_id and legacy_invalid
        ):
            request.status, request.review_code = "review", "paid_booking_requires_review"
            request.save(update_fields=["status", "review_code"])
            transaction.on_commit(
                lambda: alert_operations(f"payment-request-review:{request.pk}"), robust=True
            )
            return reservation
        request.status, request.paid_at = "paid", timezone.now()
        request.save(update_fields=["status", "paid_at"])
        reservation.payment_status = "paid"
        reservation.save(update_fields=["payment_status", "updated_at"])
        BookingIntent.objects.filter(pk=attempt.booking_intent_id).update(
            status=BookingIntent.Status.COMPLETED, updated_at=timezone.now()
        )
        transaction.on_commit(lambda: handle_reservation_confirmed(reservation.pk), robust=True)
        transaction.on_commit(lambda: queue_payment_receipt(attempt.pk), robust=True)
        transaction.on_commit(lambda: queue_delivery(request.pk, kind="receipt"), robust=True)
    return reservation


def reconcile_requests(limit=3):
    """Gateway GET recovery covers a browser closed before its return redirect."""
    if not settings.HYPERPAY_ENABLED:
        return {"checked": 0}
    checked = 0
    attempts = (
        PaymentAttempt.objects.filter(
            payment_request__isnull=False,
            payment_request__environment=settings.HYPERPAY_ENVIRONMENT,
            provider="hyperpay",
            provider_checkout_id__isnull=False,
        )
        .filter(
            Q(status__in=["created", "pending", "unknown"])
            | Q(status="succeeded", payment_request__status="open")
        )
        .exclude(provider_checkout_id="")
        .order_by("updated_at")[:limit]
    )
    if not attempts:
        return {"checked": 0}
    with HyperPayService() as service:
        for attempt in attempts:
            try:
                service.verify(attempt)
                checked += 1
            except HyperPayError:
                continue
    return {"checked": checked}
