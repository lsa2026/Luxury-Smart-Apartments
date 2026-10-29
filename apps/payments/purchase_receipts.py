"""Private payment return -> clean, session-bound receipt for future captures only."""

import logging
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import DatabaseError
from django.http import Http404, HttpResponseRedirect
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone, translation
from django.views.decorators.http import require_GET

from apps.core.marketing import (
    payment_analytics_fields,
    property_analytics_item,
    purchase_receipt_token,
    sanitize_analytics_event,
)
from apps.core.models import MarketingEventReceipt
from apps.reservations.models import Reservation

from .models import PaymentAttempt, ReservationPaymentRequest

# Forward-only release boundary. Never expose an old capture by opening its result.
START_AT = datetime(2026, 10, 29, 4, 31, tzinfo=UTC)
SESSION_KEY = "payment_link_purchase_receipt"
MAX_AGE = timedelta(days=1)
logger = logging.getLogger(__name__)


def eligible(attempt):
    if (
        settings.HYPERPAY_ENVIRONMENT != "production"
        or not attempt.payment_request_id
        or attempt.provider != "hyperpay"
        or attempt.status != "succeeded"
        or not attempt.verified_at
        or not attempt.provider_payment_id
        or attempt.created_at < START_AT
        or attempt.verified_at < START_AT
        or attempt.amount <= 0
        or attempt.currency != "SAR"
    ):
        return False
    request = ReservationPaymentRequest.objects.select_related("reservation").get(
        pk=attempt.payment_request_id
    )
    reservation = request.reservation
    return bool(
        request.status == "paid"
        and request.environment == settings.HYPERPAY_ENVIRONMENT
        and request.amount_sar == attempt.amount
        and reservation.booking_intent_id == attempt.booking_intent_id
        and reservation.normalized_status in Reservation.ACTIVE_STATUSES
        and reservation.hostaway_reservation_id
        and not reservation.is_test
    )


def receipt_for(attempt):
    """Reuse the financial receipt/HMAC/ack ledger, scoped to the actual capture."""
    reference = MarketingEventReceipt.reference_hmac(f"payment-link-capture:{attempt.pk}")
    receipt, _ = MarketingEventReceipt.objects.get_or_create(
        event_name="purchase",
        object_reference_hash=reference,
        defaults={"object_type": "PaymentAttempt", "status": "prepared"},
    )
    return receipt


def clean_redirect():
    response = HttpResponseRedirect(reverse("payments:payment_link_receipt"))
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


def redirect_verified_purchase(request, attempt):
    """Only the existing authenticated/signed payment result may mint this grant.

    The browser gets no attempt identifier or bearer token in the destination URL.
    No Google/Ads request occurs here, including on repeated financial callbacks.
    """
    if not eligible(attempt):
        return None
    try:
        receipt = receipt_for(attempt)
    except DatabaseError:
        # Measurement is secondary to an already durable successful payment.
        logger.warning("Payment-link purchase receipt could not be prepared")
        return None
    if receipt.status != "prepared":
        return None
    request.session[SESSION_KEY] = {
        "attempt": str(attempt.pk),
        "issued_at": timezone.now().isoformat(),
    }
    return clean_redirect()


@require_GET
def payment_link_receipt(request):
    # A copied clean URL grants nothing. Never enable trackers on its error page.
    request._disable_google_integrations = True
    grant = request.session.get(SESSION_KEY, {})
    try:
        issued = datetime.fromisoformat(grant["issued_at"])
        if timezone.is_naive(issued) or not timedelta(0) <= timezone.now() - issued <= MAX_AGE:
            raise ValueError("expired")
        attempt = PaymentAttempt.objects.select_related(
            "payment_request__reservation__property"
        ).get(pk=grant["attempt"])
    except (KeyError, TypeError, ValueError, PaymentAttempt.DoesNotExist) as exc:
        raise Http404 from exc
    if not eligible(attempt):
        raise Http404
    # Never render trackers in a document with an incoming query or referrer.
    # This same-URL hop strips them before any third-party script can execute.
    if request.META.get("QUERY_STRING") or request.META.get("HTTP_REFERER"):
        return clean_redirect()
    receipt = receipt_for(attempt)
    language = attempt.payment_request.language
    language = language if language in {"ar", "en", "fr"} else "ar"
    event = None
    if receipt.status == "prepared":
        item = property_analytics_item(
            attempt.payment_request.reservation.property,
            price=attempt.amount,
            currency=attempt.currency,
        )
        event = sanitize_analytics_event(
            "purchase",
            {
                "transaction_id": receipt.object_reference_hash,
                "value": attempt.amount,
                "currency": attempt.currency,
                "checkout_kind": "payment_link",
                "page_language": language,
                "items": [item],
                **payment_analytics_fields(attempt),
            },
        )
    elif receipt.status != "emitted":
        raise Http404
    request._disable_google_integrations = event is None
    request._local_public_context = True
    request.LANGUAGE_CODE = language
    from .payment_link_views import COPY

    with translation.override(language):
        response = render(
            request,
            "payments/payment_link_receipt.html",
            {
                "ui": COPY[language],
                "language": language,
                "purchase_event": event,
                "amount": attempt.amount,
                "currency": attempt.currency,
                "purchase_receipt_token": purchase_receipt_token(receipt) if event else "",
                "purchase_receipt_csrf_token": get_token(request) if event else "",
            },
        )
    response["Cache-Control"] = "private, no-store"
    # Preserve same-site CSRF evidence for acknowledgement, hide all external referrers.
    response["Referrer-Policy"] = "same-origin"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response
