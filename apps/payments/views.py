"""Owned payment pages for HyperPay TEST and the local development sandbox."""

import json
import re
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.services import claimable_reference
from apps.core.marketing import prepare_purchase_event, purchase_receipt_token
from apps.properties.models import PropertyImage
from apps.reservations.booking_forms import GuestDetailsForm
from apps.reservations.models import BookingIntent, BookingModificationRequest, BookingQuote
from apps.reservations.security import (
    grant_reservation_access, is_rate_limited, session_can_manage,
    session_key_hash, session_owns,
)
from apps.reservations.services.availability import AvailabilityRequest, AvailabilityService
from apps.reservations.services.booking import consume_revalidated_quote
from apps.reservations.signing import quote_id_from_reference, verify_quote_fingerprint

from .currency import (
    CurrencyError,
    DISPLAY_CURRENCY_SESSION_KEY,
    PAYMENT_CURRENCY,
    UnsupportedCurrencyError,
    normalize_currency,
    validate_payment_snapshot,
)
from .hyperpay.client import HyperPayClient
from .hyperpay.exceptions import HyperPayError
from .hyperpay.result_codes import HyperPayStatus, map_result_code
from .hyperpay.service import (
    HyperPayService, format_hyperpay_amount, merchant_transaction_id,
)
from .models import PaymentAttempt
from .services import simulate_booking, simulate_modification


def _enabled() -> None:
    if not settings.PAYMENT_SANDBOX_ENABLED:
        raise Http404


def _owns_intent(request: HttpRequest, intent: BookingIntent) -> bool:
    return session_owns(request, intent.session_key_hash) or (
        request.user.is_authenticated and intent.customer_id == request.user.pk
    )


def _hyperpay_enabled() -> None:
    if not settings.HYPERPAY_ENABLED:
        raise Http404


def _cover_image(property_obj: object) -> object:
    """The listing photo shown beside the amount so the payer can confirm the stay."""
    return (
        PropertyImage.objects.public()
        .filter(property=property_obj)
        .order_by("-is_cover", "sort_order", "hostaway_sort_order", "id")
        .first()
    )


def _hyperpay_return_token(attempt: PaymentAttempt) -> str:
    return signing.dumps(
        {
            "payment_id": str(attempt.pk),
            "checkout_id": attempt.provider_checkout_id,
        },
        salt="payments.hyperpay-return.v1",
        compress=True,
    )


def _valid_hyperpay_return_token(request: HttpRequest, attempt: PaymentAttempt) -> bool:
    token = request.GET.get("return_token", "")
    if not token:
        return False
    try:
        payload = signing.loads(
            token,
            salt="payments.hyperpay-return.v1",
            max_age=settings.HYPERPAY_RETURN_TOKEN_MAX_AGE_SECONDS,
        )
    except signing.BadSignature:
        return False
    return payload == {
        "payment_id": str(attempt.pk),
        "checkout_id": attempt.provider_checkout_id,
    }


def _render_hyperpay_checkout(
    request: HttpRequest,
    *,
    attempt: PaymentAttempt,
    intent: BookingIntent,
    modification: BookingModificationRequest | None = None,
) -> HttpResponse:
    """Render public widget identifiers for an already-created checkout."""

    result_url = request.build_absolute_uri(reverse("payments:hyperpay_result", args=[attempt.pk]))
    result_url = f"{result_url}?{urlencode({'return_token': _hyperpay_return_token(attempt)})}"
    property_obj = modification.reservation.property if modification else intent.property
    response = render(
        request,
        "payments/hyperpay_checkout.html",
        {
            "attempt": attempt,
            "intent": intent,
            "modification": modification,
            "cover_image": _cover_image(property_obj),
            "checkout_id": attempt.provider_checkout_id,
            "widget_integrity": attempt.widget_integrity,
            "hyperpay_environment": settings.HYPERPAY_ENVIRONMENT,
            "apple_pay_allowed": "APPLEPAY" in settings.HYPERPAY_ALLOWED_BRANDS,
            "apple_pay_standalone": (
                settings.HYPERPAY_ENVIRONMENT == "test"
                and "APPLEPAY" in settings.HYPERPAY_ALLOWED_BRANDS
            ),
            "widget_url": (
                f"{settings.HYPERPAY_BASE_URL}v1/paymentWidgets.js"
                f"?checkoutId={attempt.provider_checkout_id}"
            ),
            "result_url": result_url,
        },
    )
    response["Cache-Control"] = "no-store, private"
    return response


def _existing_checkout(**filters: object) -> PaymentAttempt | None:
    return (
        PaymentAttempt.objects.filter(
            provider="hyperpay",
            status__in=[PaymentAttempt.Status.CREATED, PaymentAttempt.Status.PENDING],
            provider_checkout_id__isnull=False,
            **filters,
        )
        .exclude(provider_checkout_id="")
        .exclude(widget_integrity="")
        .order_by("-created_at")
        .first()
    )


def _fast_checkout_enabled() -> None:
    if not (
        settings.APPLE_PAY_FAST_CHECKOUT_ENABLED
        and settings.HYPERPAY_ENABLED
        and settings.HYPERPAY_ENVIRONMENT == "test"
        and "APPLEPAY" in settings.HYPERPAY_ALLOWED_BRANDS
    ):
        raise Http404


def _fast_quote(request: HttpRequest, reference: str) -> BookingQuote:
    try:
        quote = BookingQuote.objects.select_related("property").get(
            pk=quote_id_from_reference(reference)
        )
    except (signing.BadSignature, ValueError, BookingQuote.DoesNotExist) as exc:
        raise Http404 from exc
    if not session_owns(request, quote.session_key_hash):
        raise Http404
    return quote


def _fast_session_key(quote: BookingQuote) -> str:
    return f"apple_fast_checkout_v1_{quote.pk}"


def _fast_error(message: str, *, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _fast_ready_quote(quote: BookingQuote) -> bool:
    return (
        quote.status == BookingQuote.Status.ACTIVE
        and not quote.is_expired
        and verify_quote_fingerprint(quote)
        and quote.payment_amount_sar is not None
    )


def _fast_revalidate(quote: BookingQuote) -> object:
    with AvailabilityService() as service:
        result = service.check(
            AvailabilityRequest(
                property=quote.property,
                check_in=quote.check_in,
                check_out=quote.check_out,
                guests=quote.guests,
            ),
            bypass_cache=True,
        )
    latest = result.quote
    if not (
        result.is_available
        and latest is not None
        and latest.listing_id == quote.hostaway_listing_id
        and latest.check_in == quote.check_in
        and latest.check_out == quote.check_out
        and latest.guests == quote.guests
        and latest.currency == quote.currency
        and latest.total_price == quote.total_price
    ):
        raise ValueError("quote_changed")
    return result


class ApplePayFastCreateView(View):
    """Create a TEST checkout after quote revalidation, before collecting PII."""

    http_method_names = ["post"]
    client_class = HyperPayClient

    def post(self, request: HttpRequest, reference: str) -> HttpResponse:
        _fast_checkout_enabled()
        if is_rate_limited(
            request,
            scope="apple-fast-create",
            requests=settings.BOOKING_INTENT_RATE_LIMIT_REQUESTS,
            window=settings.BOOKING_INTENT_RATE_LIMIT_WINDOW,
        ):
            return _fast_error("Please wait and try again.", status=429)
        quote = _fast_quote(request, reference)
        if request.POST.get("terms_accepted") != "on" or request.POST.get("privacy_accepted") != "on":
            return _fast_error("Accept the booking terms and privacy policy first.")
        if not _fast_ready_quote(quote):
            return _fast_error("This price has expired. Check availability again.", status=409)
        if settings.HOSTAWAY_LIVE_BOOKING_ENABLED and quote.property.hostaway_listing_map_id is None:
            return _fast_error("This property cannot be booked at present.", status=409)
        try:
            validate_payment_snapshot(
                source_amount=quote.total_price,
                source_currency=quote.currency,
                payment_amount_sar=quote.payment_amount_sar,
                snapshot=quote.exchange_rate_snapshot,
            )
            _fast_revalidate(quote)
            amount = format_hyperpay_amount(quote.payment_amount_sar)
        except (ValueError, CurrencyError, HyperPayError):
            return _fast_error("The price or availability changed. Please start a new search.", status=409)
        state = request.session.get(_fast_session_key(quote))
        if isinstance(state, dict) and state.get("checkout_id"):
            return JsonResponse({"checkoutId": state["checkout_id"]})
        merchant_id = merchant_transaction_id()
        payload = {
            "entityId": settings.HYPERPAY_ENTITY_ID,
            "amount": amount,
            "currency": PAYMENT_CURRENCY,
            "paymentType": settings.HYPERPAY_PAYMENT_TYPE,
            "merchantTransactionId": merchant_id,
            "integrity": "true",
            "testMode": "EXTERNAL",
            "customParameters[3DS2_enrolled]": "true",
        }
        try:
            with self.client_class() as client:
                response = client.create_checkout(payload)
        except HyperPayError:
            return _fast_error("Secure payment could not be started. Nothing was charged.", status=503)
        checkout_id = response.get("id")
        integrity = response.get("integrity")
        result = response.get("result")
        result_code = result.get("code") if isinstance(result, dict) else ""
        if not (
            isinstance(checkout_id, str)
            and re.fullmatch(r"[A-Za-z0-9._-]{8,255}", checkout_id)
            and isinstance(integrity, str)
            and re.fullmatch(r"sha(?:256|384|512)-[A-Za-z0-9+/=]+", integrity)
            and map_result_code(result_code) is HyperPayStatus.PENDING
        ):
            return _fast_error("Secure payment could not be started. Nothing was charged.", status=503)
        request.session[_fast_session_key(quote)] = {
            "checkout_id": checkout_id,
            "integrity": integrity,
            "merchant_id": merchant_id,
            "idempotency_key": secrets.token_urlsafe(32),
            "amount": amount,
            "terms_accepted": True,
            "privacy_accepted": True,
        }
        return JsonResponse({"checkoutId": checkout_id})


class ApplePayFastAuthorizeView(View):
    """Attach Wallet contact details to a revalidated booking before charge."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, reference: str) -> HttpResponse:
        _fast_checkout_enabled()
        quote = _fast_quote(request, reference)
        state = request.session.get(_fast_session_key(quote))
        if not isinstance(state, dict) or not state.get("checkout_id"):
            return _fast_error("The payment session expired. Please start again.", status=409)
        if len(request.body) > 8192:
            return _fast_error("Contact information is too long.")
        try:
            data = json.loads(request.body)
        except (TypeError, ValueError):
            return _fast_error("Contact information is missing.")
        if not isinstance(data, dict) or data.get("checkoutId") != state["checkout_id"]:
            return _fast_error("The payment session does not match.", status=409)
        payment = data.get("payment")
        if not isinstance(payment, dict):
            return _fast_error("Apple Pay contact information is missing.")
        shipping = payment.get("shippingContact")
        billing = payment.get("billingContact")
        if not isinstance(shipping, dict) or not isinstance(billing, dict):
            return _fast_error("Add contact and billing details in Wallet before paying.")
        lines = billing.get("addressLines")
        street = lines[0] if isinstance(lines, list) and lines and isinstance(lines[0], str) else ""
        # The TEST MPGS connector rejects non-Latin-1 billing.address values.
        address_parts = (
            street,
            billing.get("locality"),
            billing.get("administrativeArea"),
            shipping.get("givenName"),
            shipping.get("familyName"),
        )
        if any(not isinstance(value, str) or not value.isprintable() or
               any(ord(character) > 255 for character in value) for value in address_parts):
            return _fast_error("Use Latin-script name and billing address in Wallet, then try again.")
        form = GuestDetailsForm({
            "guest_first_name": shipping.get("givenName", ""),
            "guest_last_name": shipping.get("familyName", ""),
            "guest_email": shipping.get("emailAddress", ""),
            "guest_phone": shipping.get("phoneNumber", ""),
            "billing_street1": street,
            "billing_city": billing.get("locality", ""),
            "billing_state": billing.get("administrativeArea", ""),
            "billing_country": billing.get("countryCode", ""),
            "billing_postcode": billing.get("postalCode", ""),
            "terms_accepted": "on" if state.get("terms_accepted") else "",
            "privacy_accepted": "on" if state.get("privacy_accepted") else "",
            "idempotency_key": state["idempotency_key"],
        })
        if not form.is_valid():
            return _fast_error("Check your name, email, phone and billing address in Wallet.")
        existing_intent = BookingIntent.objects.filter(
            quote=quote,
            idempotency_key=state["idempotency_key"],
        ).first()
        if not existing_intent and not _fast_ready_quote(quote):
            return _fast_error("This price has expired. Please start a new search.", status=409)
        if existing_intent is None:
            try:
                revalidated = _fast_revalidate(quote)
            except (ValueError, HyperPayError):
                return _fast_error("The price or availability changed. Nothing was charged.", status=409)
            fields = form.cleaned_data
            outcome = consume_revalidated_quote(
                quote_id=quote.pk,
                session_hash=session_key_hash(request),
                idempotency_key=state["idempotency_key"],
                guest_data={
                    "guest_first_name": fields["guest_first_name"],
                    "guest_last_name": fields["guest_last_name"],
                    "guest_email": fields["guest_email"],
                    "guest_phone": fields["guest_phone"],
                    "guest_country_code": fields["billing_country"],
                    "billing_street1": fields["billing_street1"],
                    "billing_city": fields["billing_city"],
                    "billing_state": fields["billing_state"],
                    "billing_country": fields["billing_country"],
                    "billing_postcode": fields["billing_postcode"],
                    "language": request.LANGUAGE_CODE.split("-")[0] if hasattr(request, "LANGUAGE_CODE") else "ar",
                    "special_requests": "",
                    "marketing_consent": False,
                },
                revalidated=revalidated,
            )
            if outcome.intent is None:
                return _fast_error("The booking price or availability changed. Nothing was charged.", status=409)
            intent = outcome.intent
        else:
            intent = existing_intent
        attempt, created = PaymentAttempt.objects.get_or_create(
            booking_intent=intent,
            provider="hyperpay",
            provider_checkout_id=state["checkout_id"],
            defaults={
                "provider_reference": state["checkout_id"],
                "merchant_transaction_id": state["merchant_id"],
                "widget_integrity": state["integrity"],
                "amount": intent.payment_amount_sar,
                "currency": PAYMENT_CURRENCY,
                "status": PaymentAttempt.Status.PENDING,
                "idempotency_key": secrets.token_urlsafe(32),
            },
        )
        if not created and attempt.status not in (PaymentAttempt.Status.CREATED, PaymentAttempt.Status.PENDING):
            return _fast_error("This payment has already been processed.", status=409)
        return JsonResponse({"ready": True})


class ApplePayFastResultView(View):
    """Resolve the widget's checkout to the owned payment verification flow."""

    http_method_names = ["get"]

    def get(self, request: HttpRequest) -> HttpResponse:
        _fast_checkout_enabled()
        path = request.GET.get("resourcePath", "")
        match = re.fullmatch(r"/v1/checkouts/([A-Za-z0-9._-]{8,255})/payment", path)
        if match is None:
            raise Http404
        attempt = get_object_or_404(
            PaymentAttempt.objects.select_related("booking_intent"),
            provider="hyperpay",
            provider_checkout_id=match.group(1),
        )
        if not _owns_intent(request, attempt.booking_intent):
            raise Http404
        return HyperPayResultView.as_view()(request, payment_id=attempt.pk)


class CurrencyPreferenceView(View):
    """Persist a display-only preference; payment currency is never read here."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        try:
            currency = normalize_currency(request.POST.get("currency", ""))
        except UnsupportedCurrencyError:
            return HttpResponse(_("Unsupported display currency."), status=400)
        request.session[DISPLAY_CURRENCY_SESSION_KEY] = currency
        next_url = request.POST.get("next", "")
        if not url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            next_url = reverse("properties:list")
        response = redirect(next_url)
        response.set_cookie(
            settings.FX_PREFERENCE_COOKIE,
            currency,
            max_age=settings.FX_PREFERENCE_COOKIE_MAX_AGE_SECONDS,
            secure=not settings.DEBUG,
            httponly=True,
            samesite="Lax",
        )
        return response


class HyperPayBookingCheckoutView(View):
    """Create/reuse a checkout server-side, then render only its public widget data."""

    http_method_names = ["get", "post"]
    service_class = HyperPayService

    @staticmethod
    def _intent(request: HttpRequest, public_reference: str) -> BookingIntent:
        intent = get_object_or_404(
            BookingIntent.objects.select_related("property"),
            public_reference=public_reference,
        )
        if not _owns_intent(request, intent):
            raise Http404
        return intent

    def get(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        """Re-render an owned pending checkout after a display preference change."""

        _hyperpay_enabled()
        intent = self._intent(request, public_reference)
        attempt = _existing_checkout(booking_intent=intent, modification_request__isnull=True)
        if attempt is None:
            return redirect(
                "reservations:intent_detail",
                public_reference=intent.public_reference,
            )
        return _render_hyperpay_checkout(request, attempt=attempt, intent=intent)

    def post(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        _hyperpay_enabled()
        intent = self._intent(request, public_reference)
        try:
            with self.service_class() as service:
                checkout = service.create_checkout(intent)
        except HyperPayError:
            messages.error(
                request,
                _("Secure payment could not be started. Nothing was charged; please try again."),
            )
            return redirect(
                "reservations:intent_detail",
                public_reference=intent.public_reference,
            )
        return _render_hyperpay_checkout(
            request,
            attempt=checkout.attempt,
            intent=intent,
        )


class HyperPayModificationCheckoutView(View):
    """Create a checkout for a positive, freshly revalidated price difference."""

    http_method_names = ["get", "post"]
    service_class = HyperPayService

    @staticmethod
    def _modification(
        request: HttpRequest,
        public_reference: str,
    ) -> tuple[BookingModificationRequest, BookingIntent]:
        modification = get_object_or_404(
            BookingModificationRequest.objects.select_related(
                "reservation__booking_intent",
                "reservation__property",
            ),
            public_reference=public_reference,
        )
        intent = modification.reservation.booking_intent
        owns = bool(intent and _owns_intent(request, intent)) or session_can_manage(
            request,
            modification.reservation.public_reference,
        )
        if not owns or intent is None:
            raise Http404
        return modification, intent

    def get(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        _hyperpay_enabled()
        modification, intent = self._modification(request, public_reference)
        attempt = _existing_checkout(modification_request=modification)
        if attempt is None:
            return redirect(
                "reservations:modification_detail",
                public_reference=modification.public_reference,
            )
        return _render_hyperpay_checkout(
            request,
            attempt=attempt,
            intent=intent,
            modification=modification,
        )

    def post(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        _hyperpay_enabled()
        modification, intent = self._modification(request, public_reference)
        try:
            with self.service_class() as service:
                checkout = service.create_modification_checkout(modification)
        except HyperPayError:
            messages.error(
                request,
                _(
                    "The price-difference payment could not be started. Nothing was "
                    "charged, and availability was checked again."
                ),
            )
            return redirect(
                "reservations:modification_detail",
                public_reference=modification.public_reference,
            )
        return _render_hyperpay_checkout(
            request,
            attempt=checkout.attempt,
            intent=intent,
            modification=modification,
        )


class HyperPayResultView(View):
    """Treat the redirect only as a trigger for authoritative server verification."""

    http_method_names = ["get"]
    service_class = HyperPayService

    def get(self, request: HttpRequest, payment_id: object) -> HttpResponse:
        _hyperpay_enabled()
        attempt = get_object_or_404(
            PaymentAttempt.objects.select_related(
                "booking_intent",
                "modification_request__reservation",
            ),
            pk=payment_id,
            provider="hyperpay",
        )
        if not (
            _owns_intent(request, attempt.booking_intent)
            or _valid_hyperpay_return_token(request, attempt)
        ):
            raise Http404
        resource_path = request.GET.get("resourcePath", "")
        expected_path = f"/v1/checkouts/{attempt.provider_checkout_id}/payment"
        if resource_path and resource_path != expected_path:
            raise Http404
        try:
            with self.service_class() as service:
                outcome = service.verify(attempt)
        except HyperPayError:
            outcome = None
        if outcome and outcome.reservation:
            grant_reservation_access(request, outcome.reservation.public_reference)
        display_attempt = outcome.attempt if outcome else attempt
        # Offered only to a guest without an account, and only for the booking
        # this session just proved by paying for it.
        claimable = ""
        if outcome and outcome.reservation and not request.user.is_authenticated:
            claimable = claimable_reference(request, outcome.reservation.public_reference)
        purchase_event = None
        purchase_receipt = None
        if outcome and outcome.reservation:
            # This is intentionally prepared only after both authoritative
            # conditions hold: HyperPay has verified the charge and Hostaway
            # has confirmed the reservation. The browser sends no guest data.
            purchase_event, purchase_receipt = prepare_purchase_event(
                payment_attempt=display_attempt,
                reservation=outcome.reservation,
            )
        response = render(
            request,
            "payments/hyperpay_result.html",
            {
                "attempt": display_attempt,
                "outcome": outcome,
                "success": HyperPayStatus.SUCCESS,
                "claimable_reference": claimable,
                "purchase_event": purchase_event,
                "purchase_receipt_token": (
                    purchase_receipt_token(purchase_receipt)
                    if purchase_event and purchase_receipt
                    else ""
                ),
                "purchase_receipt_csrf_token": get_token(request) if purchase_event else "",
            },
            status=200 if outcome else 503,
        )
        response["Cache-Control"] = "no-store, private"
        return response


class SandboxBookingCheckoutView(View):
    http_method_names = ["get", "post"]

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        _enabled()
        return super().dispatch(request, *args, **kwargs)

    def _intent(self, request: HttpRequest, public_reference: str) -> BookingIntent:
        intent = get_object_or_404(
            BookingIntent.objects.select_related("property"),
            public_reference=public_reference,
        )
        if not _owns_intent(request, intent):
            raise Http404
        existing_reservation = getattr(intent, "reservation", None)
        if existing_reservation is not None and not existing_reservation.is_test:
            raise Http404
        return intent

    def get(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        intent = self._intent(request, public_reference)
        return render(
            request,
            "payments/sandbox_checkout.html",
            {"kind": "booking", "subject": intent, "amount": intent.total_price},
        )

    def post(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        intent = self._intent(request, public_reference)
        action = request.POST.get("action")
        if action not in {"success", "failure", "cancel"}:
            raise Http404
        result = simulate_booking(intent, action)
        if action == "success" and result.reservation is not None:
            grant_reservation_access(request, result.reservation.public_reference)
            messages.success(
                request,
                _(
                    "The local payment test completed. No financial transaction or "
                    "Hostaway operation was performed."
                ),
            )
            return redirect(
                "reservations:manage",
                public_reference=result.reservation.public_reference,
            )
        if action == "failure":
            messages.error(
                request,
                _(
                    "Payment rejection was simulated. You can test again without "
                    "any financial charge."
                ),
            )
        else:
            messages.info(
                request,
                _("Payment cancellation was simulated; the booking request did not change."),
            )
        return redirect("payments:sandbox_booking", public_reference=intent.public_reference)


class SandboxModificationCheckoutView(View):
    http_method_names = ["get", "post"]

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        _enabled()
        return super().dispatch(request, *args, **kwargs)

    def _modification(
        self, request: HttpRequest, public_reference: str
    ) -> BookingModificationRequest:
        modification = get_object_or_404(
            BookingModificationRequest.objects.select_related("reservation__booking_intent"),
            public_reference=public_reference,
        )
        intent = modification.reservation.booking_intent
        if not modification.reservation.is_test:
            raise Http404
        owns = bool(intent and _owns_intent(request, intent)) or session_can_manage(
            request, modification.reservation.public_reference
        )
        if not owns:
            raise Http404
        return modification

    def get(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        modification = self._modification(request, public_reference)
        if modification.price_difference <= 0:
            raise Http404
        return render(
            request,
            "payments/sandbox_checkout.html",
            {
                "kind": "modification",
                "subject": modification,
                "amount": modification.price_difference,
            },
        )

    def post(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        modification = self._modification(request, public_reference)
        action = request.POST.get("action")
        if action not in {"success", "failure", "cancel"}:
            raise Http404
        simulate_modification(modification, action)
        if action == "success":
            messages.success(
                request,
                _(
                    "The local price-difference payment test completed. The original "
                    "booking did not change, and nothing was sent to Hostaway."
                ),
            )
        elif action == "failure":
            messages.error(
                request,
                _("Price-difference payment rejection was simulated without any charge."),
            )
        else:
            messages.info(
                request,
                _("The simulation was cancelled; the original booking remains unchanged."),
            )
        return redirect(
            "reservations:modification_detail",
            public_reference=modification.public_reference,
        )
