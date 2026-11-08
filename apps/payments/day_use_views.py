"""Signed, payment-only day_use pages; GET never creates a checkout."""

from types import SimpleNamespace

from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone, translation
from django.views import View

from apps.core.guest_documents import guest_documents
from apps.reservations.address_places import (
    CheckoutAddressView,
    make_address_ticket,
    places_enabled,
)
from apps.reservations.day_use_billing import saved_billing
from apps.reservations.day_use_stays import DayUseStayError
from apps.reservations.security import is_ip_rate_limited, session_key_hash

from .day_use_payments import create_checkout, require_payable, resolve_token, verify_payment
from .day_use_validation import DayUsePaymentForm, copy, customer_values
from .hyperpay.exceptions import HyperPayError
from .models import DayUsePaymentAttempt
from .payment_link_views import private_response
from .views import _render_hyperpay_checkout, _valid_hyperpay_return_token


class DayUsePaymentView(View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, token):
        if not settings.HYPERPAY_ENABLED:
            raise Http404
        self.token = token
        self.stay = resolve_token(token)
        if self.stay is None:
            raise Http404
        request._disable_google_integrations = True
        self.language = "en" if self.stay.guest_language == "en" else "ar"
        request.LANGUAGE_CODE = self.language
        with translation.override(self.language):
            return private_response(super().dispatch(request, token), form_page=True)

    def page(self, request, form=None, error=""):
        try:
            require_payable(self.stay)
            payable = True
        except HyperPayError:
            payable = False
        address = saved_billing(self.stay)
        address_errors = None
        if self.stay.billing_address and not address:
            from apps.reservations.day_use_billing import DayUseBillingForm

            address_errors = DayUseBillingForm(self.stay.billing_address)
            address_errors.is_valid()
            payable = False
        context = {
            "stay": self.stay,
            "payable": payable,
            "form": form
            if form is not None
            else DayUsePaymentForm(initial=customer_values(self.stay), default_country_code="SA"),
            "saved_billing": address,
            "guest_documents": guest_documents(self.language),
            "current_language_code": self.language,
            "address_errors": address_errors,
            "error": error,
            "address_places_enabled": payable and not address and places_enabled(),
        }
        if context["address_places_enabled"]:
            context.update(
                address_places_ticket=make_address_ticket(
                    day_use_address_scope(request, self.stay)
                ),
                address_suggestions_url=reverse(
                    "payments:day_use_address_suggestions", args=[self.token]
                ),
                address_details_url=reverse("payments:day_use_address_details", args=[self.token]),
            )
        return render(request, "payments/day_use_payment.html", context)

    def get(self, request, token):
        return self.page(request)

    def post(self, request, token):
        if is_ip_rate_limited(request, scope="day_use-payment", requests=8, window=60):
            return private_response(
                render(
                    request,
                    "payments/day_use_result.html",
                    {
                        "message": copy(
                            "طلبات متكررة؛ انتظر قبل المحاولة مجددًا.",
                            "Too many requests. Wait before trying again.",
                        )
                    },
                    status=429,
                )
            )
        data = request.POST.copy()
        for name, value in customer_values(self.stay).items():
            data.setdefault(name, value)
        address = saved_billing(self.stay)
        if address:
            data.update(address)
        form = DayUsePaymentForm(data)
        if not form.is_valid():
            return self.page(request, form=form)
        acceptance = {
            "source": "day_use_payment_link",
            "documents": guest_documents(self.language),
            "documents_digest": form.cleaned_data["documents_digest"],
            "consents": {"terms": True, "house_rules": True},
            "accepted_at": timezone.now().isoformat(),
            "starts_at": self.stay.starts_at.isoformat(),
            "ends_at": self.stay.ends_at.isoformat(),
            "amount_sar": str(self.stay.amount_sar),
        }
        try:
            attempt = create_checkout(
                self.stay.pk,
                billing_data={
                    k: v for k, v in form.cleaned_data.items() if k.startswith("billing_")
                },
                acceptance=acceptance,
                customer_data={
                    k: v for k, v in form.cleaned_data.items() if k.startswith("guest_")
                },
            )
        except DayUseStayError as exc:
            return self.page(
                request,
                form=form,
                error=copy(
                    str(exc),
                    "Availability could not be confirmed. Contact management before paying.",
                ),
            )
        except (HyperPayError, ValueError):
            return self.page(
                request,
                form=form,
                error=copy(
                    "تعذر بدء الدفع أو توجد محاولة غير محسومة. "
                    "لا تدفع مجددًا قبل التحقق مع الإدارة.",
                    "Payment could not be started or an earlier attempt is unresolved. "
                    "Contact management before paying again.",
                ),
            )
        return _render_hyperpay_checkout(
            request,
            attempt=attempt,
            day_use_stay=self.stay,
            intent=SimpleNamespace(
                property=self.stay.property, total_price=self.stay.amount_sar, currency="SAR"
            ),
        )


class DayUsePaymentResultView(View):
    http_method_names = ["get"]

    def dispatch(self, request, payment_id):
        self.attempt = get_object_or_404(
            DayUsePaymentAttempt.objects.select_related("stay"), pk=payment_id
        )
        lang = "en" if self.attempt.stay.guest_language == "en" else "ar"
        request.LANGUAGE_CODE = lang
        with translation.override(lang):
            return super().dispatch(request, payment_id)

    def get(self, request, payment_id):
        if not settings.HYPERPAY_ENABLED:
            raise Http404
        attempt = self.attempt
        if not _valid_hyperpay_return_token(request, attempt):
            raise Http404
        expected_path = f"/v1/checkouts/{attempt.provider_checkout_id}/payment"
        if request.GET.get("resourcePath", expected_path) != expected_path:
            raise Http404
        request._disable_google_integrations = True
        try:
            attempt = verify_payment(attempt.pk)
        except HyperPayError:
            pass
        message = {
            "succeeded": copy(
                "تم التحقق من استلام دفعتك للحجز النهاري. لا تدفع مرة أخرى.",
                "Your day_use stay payment is verified. Do not pay again.",
            ),
            "failed": copy(
                "لم تنجح الدفعة. راجع الإدارة قبل إعادة المحاولة.",
                "Payment failed. Contact management before trying again.",
            ),
            "cancelled": copy(
                "أُلغيت محاولة الدفع؛ لم يُثبت سداد الحجز.",
                "The payment attempt was cancelled; payment is not confirmed.",
            ),
        }.get(
            attempt.status,
            copy(
                "حالة الدفع قيد التحقق أو المراجعة. لا تدفع مرة أخرى الآن.",
                "Payment is being verified or reviewed. Do not pay again now.",
            ),
        )
        if attempt.status == "succeeded" and attempt.stay.status == "review":
            message = copy(
                "استُلمت الدفعة، لكن توقيت الدخول يحتاج تأكيد الإدارة. لا تدفع مرة أخرى.",
                "Payment received; management must confirm admission. Do not pay again.",
            )
        return private_response(
            render(request, "payments/day_use_result.html", {"message": message})
        )


def day_use_address_scope(request, stay):
    # No guest identity or bearer token is sent to the provider.
    return SimpleNamespace(
        pk=f"day_use-payment:{stay.pk}", session_key_hash=session_key_hash(request)
    )


class DayUsePaymentAddressView(CheckoutAddressView):
    def dispatch(self, request, reference):
        if not settings.HYPERPAY_ENABLED:
            raise Http404
        self.stay = resolve_token(reference)
        if self.stay is None or saved_billing(self.stay):
            raise Http404
        try:
            require_payable(self.stay)
        except HyperPayError as exc:
            raise Http404 from exc
        request._disable_google_integrations = True
        lang = "en" if self.stay.guest_language == "en" else "ar"
        request.LANGUAGE_CODE = lang
        with translation.override(lang):
            return private_response(super().dispatch(request, reference), form_page=True)

    def get_address_scope(self, request, reference):
        return day_use_address_scope(request, self.stay)
