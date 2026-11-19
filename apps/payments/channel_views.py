"""Every collection and gateway action requires the existing operations session."""

from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.access import operations_access_required
from apps.integrations.hostaway.exceptions import HostawayError
from apps.reservations.morning_billing import address_context
from apps.reservations.security import is_ip_rate_limited

from .channel_collections import (
    activation_ready,
    create_collection,
    reconcile,
    refresh_balance,
    start_checkout,
)
from .channel_forms import ChannelCollectionForm
from .hyperpay.exceptions import HyperPayError
from .models import ChannelCardCollection
from .payment_link_views import private_response
from .views import _hyperpay_return_token, _valid_hyperpay_return_token


def _private(request):
    request._disable_google_integrations = True


@operations_access_required
@require_http_methods(["GET", "POST"])
def collection_list(request):
    _private(request)
    form = ChannelCollectionForm(
        request.POST if request.method == "POST" else None,
        initial={"available_on": timezone.localdate()},
    )
    if request.method == "POST" and form.is_valid():
        if is_ip_rate_limited(request, scope="channel-collection-create", requests=8, window=60):
            form.add_error(None, "طلبات متكررة؛ انتظر قبل إعادة القراءة.")
        else:
            try:
                order = create_collection(request.POST, actor=request.user)
                return private_response(
                    redirect("notifications:channel_collection_detail", collection_id=order.pk)
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            except HostawayError:
                form.add_error(None, "تعذرت قراءة الحجز من هوستاوي؛ لم تنشأ جلسة دفع أو تحصيل.")
    return private_response(
        render(
            request,
            "admin/payments/channel_list.html",
            {
                "title": "تحصيل بطاقات بوكينج",
                "form": form,
                "collections": ChannelCardCollection.objects.select_related("property")[:50],
                "activation_ready": activation_ready(),
                **address_context(request),
            },
        ),
        form_page=True,
    )


@operations_access_required
@require_http_methods(["GET", "POST"])
def collection_detail(request, collection_id):
    _private(request)
    order = get_object_or_404(
        ChannelCardCollection.objects.select_related("property", "created_by"), pk=collection_id
    )
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "refresh" and request.POST.get("balance_confirmed") == "on":
                refresh_balance(order.pk, actor=request.user, balance_confirmed=True)
                messages.success(request, "حُفظ تأكيدك الجديد لرصيد بوكينج؛ لم يبدأ خصم.")
            elif action == "start":
                if is_ip_rate_limited(
                    request, scope="channel-collection-start", requests=4, window=60
                ):
                    raise ValidationError("انتظر قبل طلب فتح البوابة مجددًا.")
                order = start_checkout(order.pk, actor=request.user)
                request._hyperpay_checkout_enabled = True
                result_url = request.build_absolute_uri(
                    reverse("notifications:channel_collection_result", args=[order.pk])
                )
                result_url += "?" + urlencode({"return_token": _hyperpay_return_token(order)})
                return private_response(
                    render(
                        request,
                        "admin/payments/channel_checkout.html",
                        {
                            "title": "تحصيل بطاقة بوكينج",
                            "order": order,
                            "result_url": result_url,
                            "widget_url": settings.HYPERPAY_WIDGET_ORIGIN
                            + "/v1/paymentWidgets.js?"
                            + urlencode({"checkoutId": order.provider_checkout_id}),
                            "widget_integrity": order.widget_integrity,
                            "test_environment": order.environment == "test",
                        },
                    ),
                    form_page=True,
                )
            elif action == "reconcile":
                if is_ip_rate_limited(
                    request, scope="channel-collection-reconcile", requests=10, window=60
                ):
                    raise ValidationError("انتظر قبل طلب حالة البوابة مجددًا.")
                reconcile(order.pk, actor=request.user)
                messages.success(request, "حُدثت حالة التحصيل من الخادم؛ لم يُرسل طلب خصم جديد.")
            else:
                raise ValidationError("أكد مراجعة رصيد بوكينج قبل تحديثه، أو اختر الإجراء الصحيح.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        except (HyperPayError, HostawayError):
            messages.error(
                request,
                "تعذر تأكيد نتيجة البوابة أو قراءة الحجز. "
                "لا تعد الخصم؛ افتح هذا الأمر للتحقق والمراجعة.",
            )
        return private_response(
            redirect("notifications:channel_collection_detail", collection_id=order.pk)
        )
    link = request.build_absolute_uri(
        reverse("notifications:channel_collection_detail", args=[order.pk])
    )
    return private_response(
        render(
            request,
            "admin/payments/channel_detail.html",
            {
                "title": "أمر تحصيل بطاقة بوكينج",
                "order": order,
                "private_link": link,
                "activation_ready": activation_ready(),
            },
        ),
        form_page=True,
    )


@operations_access_required
@require_http_methods(["GET"])
def collection_result(request, collection_id):
    _private(request)
    order = get_object_or_404(ChannelCardCollection, pk=collection_id)
    if not _valid_hyperpay_return_token(request, order):
        return private_response(
            HttpResponseForbidden(
                "رابط العودة غير صالح. افتح أمر التحصيل من الإدارة وتحقق من حالته."
            )
        )
    if is_ip_rate_limited(request, scope="channel-collection-return", requests=10, window=60):
        messages.error(request, "انتظر قبل إعادة التحقق؛ لا تعد الدفع.")
    else:
        try:
            reconcile(order.pk, actor=request.user)
        except (ValidationError, HyperPayError):
            messages.error(
                request, "لم تُحسم نتيجة البوابة؛ لا تعد الدفع. استخدم التحقق من داخل أمر التحصيل."
            )
    # Drop gateway query strings and tokens before showing any operational page.
    return private_response(
        redirect("notifications:channel_collection_detail", collection_id=order.pk)
    )
