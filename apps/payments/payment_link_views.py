"""Public payment-only capability; no login or booking-management grant."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone, translation
from django.views import View

from apps.core.guest_documents import guest_documents
from apps.integrations.hostaway.exceptions import HostawayError
from apps.payments.currency import CurrencyError
from apps.reservations.address_places import (
    CheckoutAddressView,
    make_address_ticket,
    places_enabled,
)
from apps.reservations.booking_forms import GuestDetailsForm
from apps.reservations.security import is_ip_rate_limited, session_key_hash
from apps.reservations.views import _rate_conditions_snapshot

from .hyperpay.exceptions import HyperPayError
from .models import ReservationPaymentRequest
from .payment_links import create_request_checkout, link_token, resolve_token, validate_open
from .views import _render_hyperpay_checkout

COPY = {
    "ar": {
        "title": "سداد الحجز",
        "pay": "متابعة إلى الدفع الآمن",
        "billing": "عنوان الفوترة",
        "unavailable": "رابط السداد غير متاح",
        "closed": "الرابط موقوف أو تم سداد الحجز أو تغيّرت حالته. تواصل مع الإدارة.",
        "error": "تعذر بدء الدفع الآن. إذا بدأت محاولة سابقة فلا تدفع مجددًا قبل التحقق.",
        "paid": "تم التحقق من استلام دفعتك. لا تدفع مرة أخرى.",
        "pending": "التحقق من الدفع قيد المتابعة. لا تدفع مرة أخرى قبل تأكيد الحالة.",
        "failed": "لم يتأكد نجاح الدفع. الحجز القائم لم يُلغَ. تواصل مع الإدارة.",
        "review": "الدفع متحقق، لكن الحجز يحتاج مراجعة الإدارة. لا تدفع مرة أخرى.",
        "conditions": "شروط السعر والإلغاء",
        "total": "المبلغ المطلوب بالريال السعودي",
        "contact": "التواصل مع الإدارة",
    },
    "en": {
        "title": "Booking payment",
        "pay": "Continue to secure payment",
        "billing": "Billing address",
        "unavailable": "Payment link unavailable",
        "closed": "This link is stopped, already paid or its booking changed. Contact management.",
        "error": (
            "Payment could not be started. Do not pay again before checking an earlier attempt."
        ),
        "paid": "Your payment has been verified. Please do not pay again.",
        "pending": "Payment verification is in progress. Do not pay again before confirmation.",
        "failed": "Payment success was not confirmed. Your existing booking was not cancelled.",
        "review": "Payment verified; the booking needs management review. Do not pay again.",
        "conditions": "Rate and cancellation conditions",
        "total": "Amount due in Saudi riyals",
        "contact": "Contact management",
    },
    "fr": {
        "title": "Paiement de la réservation",
        "pay": "Continuer vers le paiement sécurisé",
        "billing": "Adresse de facturation",
        "unavailable": "Lien de paiement indisponible",
        "closed": (
            "Ce lien est arrêté, déjà payé ou la réservation a changé. Contactez la direction."
        ),
        "error": "Le paiement n’a pas pu démarrer. Vérifiez toute tentative avant de repayer.",
        "paid": "Votre paiement est vérifié. Ne payez pas à nouveau.",
        "pending": "Vérification en cours. Ne payez pas avant confirmation.",
        "failed": "Le paiement n’est pas confirmé. La réservation existante n’a pas été annulée.",
        "review": "Paiement vérifié ; la réservation nécessite une vérification. Ne repayez pas.",
        "conditions": "Conditions tarifaires et d’annulation",
        "total": "Montant dû en riyals saoudiens",
        "contact": "Contacter la direction",
    },
}


class PaymentLinkBillingForm(GuestDetailsForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in list(self.fields):
            if not (
                name.startswith("billing_")
                or name in {"terms_accepted", "house_rules_accepted", "documents_digest"}
            ):
                self.fields.pop(name)


def private_response(response, *, form_page=False):
    response["Cache-Control"] = "no-store, private"
    # no-referrer can make Safari send Origin: null on a same-origin form POST.
    # Same-origin preserves CSRF validation without leaking the capability to
    # HyperPay, Google or any other cross-origin destination. Results need none.
    response["Referrer-Policy"] = "same-origin" if form_page else "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


class ReservationPaymentView(View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, token):
        if not settings.RESERVATION_PAYMENT_LINKS_ENABLED or not settings.HYPERPAY_ENABLED:
            raise Http404
        payment_request = resolve_token(token)
        if payment_request is None:
            raise Http404
        self.payment_request = payment_request
        language = payment_request.language if payment_request.language in COPY else "ar"
        request.LANGUAGE_CODE = language
        # Bearer URLs must never enter analytics, marketing or third-party map URLs.
        request._disable_google_integrations = True
        with translation.override(language):
            return private_response(super().dispatch(request, token), form_page=True)

    def _render(self, request, *, form=None, error=""):
        payment_request = self.payment_request
        payable = False
        if payment_request.status == "open":
            try:
                validate_open(payment_request)
                payable = True
            except (ValueError, ValidationError, CurrencyError, HyperPayError):
                pass
        ui = COPY[request.LANGUAGE_CODE]
        context = {
            "payment_request": payment_request,
            "ui": ui,
            "payable": payable,
            "form": form
            or PaymentLinkBillingForm(
                default_country_code=payment_request.reservation.booking_intent.billing_country
            ),
            "guest_documents": guest_documents(request.LANGUAGE_CODE),
            "error": error,
            "conditions": _rate_conditions_snapshot(
                payment_request.reservation.booking_intent.quote
            ),
            "address_places_enabled": payable and places_enabled(),
        }
        if context["address_places_enabled"]:
            context.update(
                address_places_ticket=make_address_ticket(_address_scope(request, payment_request)),
                address_suggestions_url=reverse(
                    "payments:reservation_address_suggestions", args=[link_token(payment_request)]
                ),
                address_details_url=reverse(
                    "payments:reservation_address_details", args=[link_token(payment_request)]
                ),
            )
        return render(request, "payments/reservation_payment.html", context)

    def get(self, request, token):
        # Link preview / GET never calls any provider, consumes a token or grants ownership.
        return self._render(request)

    def post(self, request, token):
        if is_ip_rate_limited(request, scope="reservation-payment", requests=8, window=60):
            return render(
                request,
                "payments/reservation_payment_result.html",
                {
                    "ui": COPY[request.LANGUAGE_CODE],
                    "message": COPY[request.LANGUAGE_CODE]["pending"],
                },
                status=429,
            )
        form = PaymentLinkBillingForm(request.POST)
        if not form.is_valid():
            return self._render(request, form=form)
        accepted = {
            "source": "reservation_payment_link",
            "documents": guest_documents(),
            "documents_digest": form.cleaned_data["documents_digest"],
            "consents": {"terms": True, "house_rules": True},
            "accepted_at": timezone.now().isoformat(),
            "rate_conditions": {
                **_rate_conditions_snapshot(self.payment_request.reservation.booking_intent.quote),
                "total": str(self.payment_request.source_amount),
                "currency": self.payment_request.source_currency,
                "payment_amount_sar": str(self.payment_request.amount_sar),
            },
        }
        try:
            checkout = create_request_checkout(
                self.payment_request.pk, billing_data=form.cleaned_data, acceptance=accepted
            )
        except (HyperPayError, HostawayError, CurrencyError, ValidationError, ValueError):
            return self._render(request, form=form, error=COPY[request.LANGUAGE_CODE]["error"])
        return _render_hyperpay_checkout(
            request,
            attempt=checkout.attempt,
            intent=self.payment_request.reservation.booking_intent,
            payment_request=self.payment_request,
        )


def _address_scope(request, payment_request):
    from types import SimpleNamespace

    # Separate namespace and this browser's own marker, never ownership of the
    # original quote/session. Opening a payment link grants no booking access.
    return SimpleNamespace(
        pk=f"payment:{payment_request.pk}", session_key_hash=session_key_hash(request)
    )


class ReservationPaymentAddressView(CheckoutAddressView):
    def dispatch(self, request, reference):
        if not settings.RESERVATION_PAYMENT_LINKS_ENABLED or not settings.HYPERPAY_ENABLED:
            raise Http404
        payment_request = resolve_token(reference)
        if payment_request is None:
            raise Http404
        try:
            validate_open(payment_request)
        except (ValueError, ValidationError, CurrencyError, HyperPayError) as exc:
            raise Http404 from exc
        self.payment_request = payment_request
        language = payment_request.language if payment_request.language in COPY else "ar"
        request.LANGUAGE_CODE = language
        request._disable_google_integrations = True
        # Renewals and post-selection tickets must retain the link's language,
        # independently of Accept-Language or the site's language cookie.
        with translation.override(language):
            return private_response(super().dispatch(request, reference), form_page=True)

    def get_address_scope(self, request, reference):
        return _address_scope(request, self.payment_request)


def render_request_result(request, attempt, outcome):
    payment_request = ReservationPaymentRequest.objects.get(pk=attempt.payment_request_id)
    language = payment_request.language if payment_request.language in COPY else "ar"
    request.LANGUAGE_CODE = language
    request._disable_google_integrations = True
    ui = COPY[language]
    if attempt.status in {"refunded", "partially_refunded"}:
        message = ui["pending"]
    elif payment_request.status == "paid":
        message = ui["paid"]
    elif attempt.status == "succeeded" or payment_request.status == "review":
        message = ui["review"]
    elif outcome is None or attempt.status in {"created", "pending", "unknown", "review"}:
        message = ui["pending"]
    else:
        message = ui["failed"]
    with translation.override(language):
        return private_response(
            render(
                request,
                "payments/reservation_payment_result.html",
                {
                    "ui": ui,
                    "message": message,
                    "payment_request": payment_request,
                },
            )
        )
