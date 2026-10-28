"""Private administrator review and explicit approval of an amended payment link."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import translation

from apps.accounts.access import operations_access_required, require_operations_access
from apps.notifications.services.audit import record_audit

from .currency import CurrencyError
from .hyperpay.exceptions import HyperPayError
from .models import ReservationPaymentRequest
from .payment_link_revisions import approve_preview, prepare_preview, projected_reservation
from .payment_link_tasks import payment_message, queue_delivery
from .payment_links import public_url

ERRORS = {
    "previous_payment_unresolved": "توجد محاولة دفع غير محسومة. تحقق من نتيجتها قبل تحديث الرابط.",
    "nothing_due": "لا يوجد مبلغ متبقٍ للتحصيل. راجع تأكيد التعديل أو الاسترداد.",
    "expired": "انتهت صلاحية فحص التعديل. أعد فحص السعر والتوفر ثم اعتماد السعر.",
    "final_price_required": "اعتمد السعر النهائي للتعديل أولًا.",
    "payment_preview_changed": "تغيّر الحجز أو المبلغ أو انتهت المعاينة. أنشئ معاينة جديدة.",
    "approved_payment_balance_changed": "تغيّرت الدفعات أو بيانات الحجز. راجع المبلغ قبل الإرسال.",
    "payment_links_disabled": "روابط سداد الموقع غير مفعّلة حاليًا.",
    "unmatched_hostaway_collection": "يوجد سداد في Hostaway يحتاج مطابقة قبل طلب مبلغ إضافي.",
}


def _render_review(request, context):
    with translation.override("ar"):
        return render(request, "admin/reservations/payment_link_review.html", context)


def _error(request, exc):
    code = getattr(exc, "code", str(exc))
    messages.error(
        request,
        ERRORS.get(
            code,
            "تعذر اعتماد رابط الدفع أو إرساله. راجع حالة الحجز والسداد، ثم أعد المعاينة. "
            "قد يكون تعديل الحجز تأكد في Hostaway؛ لا تنشئ حجزًا آخر.",
        ),
    )


@operations_access_required
def preview_updated_link(request, reservation_id):
    require_operations_access(request)
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    from apps.reservations.models import BookingModificationRequest

    modification = get_object_or_404(
        BookingModificationRequest,
        pk=request.POST.get("modification_id"),
        reservation_id=reservation_id,
    )
    try:
        preview = prepare_preview(reservation_id, modification.pk)
    except (HyperPayError, CurrencyError, ValueError, ValidationError) as exc:
        _error(request, exc)
        return redirect(
            reverse("notifications:booking_detail", args=[reservation_id]) + "?section=edit"
        )
    record_audit(
        request=request,
        action="payment_link.revision_previewed",
        object_type="ReservationPaymentRequest",
        object_reference=str(preview.pk),
        summary="Administrator reviewed an inactive payment-link update; no message or charge.",
    )
    return redirect("notifications:updated_payment_link_review", reservation_id, preview.pk)


@operations_access_required
def updated_link_review(request, reservation_id, preview_id):
    require_operations_access(request)
    preview = get_object_or_404(
        ReservationPaymentRequest.objects.select_related(
            "reservation__property", "reservation__booking_intent", "modification_request"
        ),
        pk=preview_id,
        reservation_id=reservation_id,
        modification_request__isnull=False,
    )
    if request.method == "POST":
        if request.POST.get("confirm_updated_link") != "on":
            messages.error(request, "راجع المبلغ والرسالة وأكّد الاعتماد قبل تحديث الرابط وإرساله.")
        else:
            try:
                active = approve_preview(preview.pk)
                delivery = queue_delivery(active.pk)
                record_audit(
                    request=request,
                    action="payment_link.revision_approved",
                    object_type="ReservationPaymentRequest",
                    object_reference=str(active.pk),
                    summary="Owner approved the amended stay/payment balance and guest delivery.",
                    metadata={"amount_sar": str(active.amount_sar), "delivery": delivery.status},
                )
                messages.success(
                    request,
                    "اعتُمد تعديل الحجز وتحديث الرابط. حالة رسالة الضيف: "
                    + {
                        "queued": "بانتظار الإرسال",
                        "sending": "جارٍ الإرسال",
                        "sent": "قبل مزود الواتساب الرسالة",
                    }.get(delivery.status, "تحتاج مراجعة سجل الإرسال"),
                )
            except (HyperPayError, CurrencyError, ValueError, ValidationError) as exc:
                _error(request, exc)
            except Exception:
                # An uncertain provider/worker response is not permission to
                # repeat the Hostaway write or synchronously send a message.
                messages.error(request, "تحتاج نتيجة الاعتماد أو الإرسال مراجعة سجل الحجز والدفع.")
            return redirect("notifications:updated_payment_link_review", reservation_id, preview.pk)
    preview.refresh_from_db()
    projected = projected_reservation(preview.modification_request)
    preview.reservation = projected
    snapshot = preview.settlement_snapshot
    response = _render_review(
        request,
        {
            "title": "مراجعة تحديث رابط الدفع",
            "preview": preview,
            "projected": projected,
            "balance_total": snapshot["total"],
            "balance_paid": snapshot["paid"],
            "balance_due": snapshot["due"],
            "payment_url": public_url(preview),
            "message_preview": payment_message(preview),
            "deliveries": preview.deliveries.all(),
        },
    )
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
