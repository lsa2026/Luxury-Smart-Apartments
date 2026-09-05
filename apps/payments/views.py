"""Owned payment pages for HyperPay TEST and the local development sandbox."""

from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.services import claimable_reference
from apps.properties.models import PropertyImage
from apps.reservations.models import BookingIntent, BookingModificationRequest
from apps.reservations.security import grant_reservation_access, session_can_manage, session_owns

from .currency import (
    DISPLAY_CURRENCY_SESSION_KEY,
    UnsupportedCurrencyError,
    normalize_currency,
)
from .hyperpay.exceptions import HyperPayError
from .hyperpay.result_codes import HyperPayStatus
from .hyperpay.service import HyperPayService
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
        response = render(
            request,
            "payments/hyperpay_result.html",
            {
                "attempt": display_attempt,
                "outcome": outcome,
                "success": HyperPayStatus.SUCCESS,
                "claimable_reference": claimable,
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
