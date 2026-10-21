"""Signed, payment-only morning pages; GET never creates a checkout."""

from types import SimpleNamespace

from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone, translation
from django.views import View

from apps.core.guest_documents import guest_documents
from apps.reservations.morning_stays import MorningStayError
from apps.reservations.security import is_ip_rate_limited

from .hyperpay.exceptions import HyperPayError
from .models import MorningPaymentAttempt
from .morning_payments import create_checkout, require_payable, resolve_token, verify_payment
from .payment_link_views import PaymentLinkBillingForm, private_response
from .views import _render_hyperpay_checkout, _valid_hyperpay_return_token


class MorningPaymentView(View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, token):
        if not settings.HYPERPAY_ENABLED:
            raise Http404
        self.stay = resolve_token(token)
        if self.stay is None:
            raise Http404
        request._disable_google_integrations = True
        request.LANGUAGE_CODE = "ar"
        with translation.override("ar"):
            return private_response(super().dispatch(request, token), form_page=True)

    def page(self, request, form=None, error=""):
        try:
            require_payable(self.stay)
            payable = True
        except HyperPayError:
            payable = False
        return render(
            request,
            "payments/morning_payment.html",
            {
                "stay": self.stay,
                "payable": payable,
                "form": form or PaymentLinkBillingForm(default_country_code="SA"),
                "guest_documents": guest_documents("ar"),
                "error": error,
            },
        )

    def get(self, request, token):
        return self.page(request)

    def post(self, request, token):
        if is_ip_rate_limited(request, scope="morning-payment", requests=8, window=60):
            return private_response(
                render(
                    request,
                    "payments/morning_result.html",
                    {"message": "طلبات متكررة؛ انتظر قبل المحاولة مجددًا."},
                    status=429,
                )
            )
        form = PaymentLinkBillingForm(request.POST)
        if not form.is_valid():
            return self.page(request, form=form)
        acceptance = {
            "source": "morning_payment_link",
            "documents": guest_documents("ar"),
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
            )
        except MorningStayError as exc:
            return self.page(request, form=form, error=str(exc))
        except (HyperPayError, ValueError):
            return self.page(
                request,
                form=form,
                error=(
                    "تعذر بدء الدفع أو توجد محاولة غير محسومة. لا تدفع مجددًا قبل التحقق مع الإدارة."
                ),
            )
        return _render_hyperpay_checkout(
            request,
            attempt=attempt,
            morning_stay=self.stay,
            intent=SimpleNamespace(
                property=self.stay.property, total_price=self.stay.amount_sar, currency="SAR"
            ),
        )


class MorningPaymentResultView(View):
    http_method_names = ["get"]

    def get(self, request, payment_id):
        if not settings.HYPERPAY_ENABLED:
            raise Http404
        attempt = get_object_or_404(MorningPaymentAttempt, pk=payment_id)
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
            "succeeded": "تم التحقق من استلام دفعتك للحجز الصباحي. لا تدفع مرة أخرى.",
            "failed": "لم تنجح الدفعة. راجع الإدارة قبل إعادة المحاولة.",
            "cancelled": "أُلغيت محاولة الدفع؛ لم يُثبت سداد الحجز.",
        }.get(attempt.status, "حالة الدفع قيد التحقق أو المراجعة. لا تدفع مرة أخرى الآن.")
        if attempt.status == "succeeded" and attempt.stay.status == "review":
            message = "استُلمت الدفعة، لكن توقيت الدخول يحتاج تأكيد الإدارة. لا تدفع مرة أخرى."
        return private_response(
            render(request, "payments/morning_result.html", {"message": message})
        )
