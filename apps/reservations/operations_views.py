"""Owner-only operations screens for preparing manual booking drafts."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.access import require_operations_owner
from apps.notifications.models import AuditLog, WhatsAppDelivery
from apps.notifications.services.audit import record_audit
from apps.notifications.services.ultramsg import (
    send_manual_payment_link_request,
    send_modification_payment_link_request,
)
from apps.payments.hyperpay.refunds import HyperPayRefundService
from apps.payments.models import PaymentAttempt
from apps.properties.models import Property

from .manual_bookings import (
    create_manual_booking_draft,
    create_manual_booking_in_hostaway,
    finalize_manual_booking_draft,
    recheck_manual_booking_draft,
)
from .models import (
    BookingModificationRequest,
    BookingQuote,
    ManualBookingDraft,
    RefundObligation,
    Reservation,
)
from .modification_forms import DateChangeRequestForm
from .operations_forms import (
    CancellationDecisionForm,
    CancellationExecutionForm,
    CancellationRejectionForm,
    ManualBookingAvailabilityForm,
    ManualBookingCancelForm,
    ManualBookingDeleteForm,
    ManualBookingFinalizeForm,
    OwnerModificationExecutionForm,
    RefundDecisionForm,
    RefundGatewaySubmitForm,
    RefundSettlementForm,
)
from .services.automatic_modifications import execute_automatic_modification
from .services.availability import AvailabilityRequest, AvailabilityService
from .services.modifications import ModificationService
from .services.refunds import cancellation_refund, successful_original_payment_amount

logger = logging.getLogger(__name__)


def _require_owner(request: HttpRequest) -> None:
    require_operations_owner(request.user)


_MANUAL_DRAFT_AUDIT_LABELS = {
    "manual_booking.availability_checked": "تم فحص التوفر وتثبيت سعر النظام.",
    "manual_booking.rechecked": "أُعيد فحص التوفر والسعر من Hostaway لهذه المسودة.",
    "manual_booking.ready_for_payment": "تم اعتماد بيانات الضيف والسعر النهائي.",
    "manual_booking.hostaway_created": "أُنشئ الحجز في Hostaway وهو بانتظار الدفع.",
    "manual_booking.accounting_whatsapp_sent": (
        "أُرسل طلب رابط الدفع تلقائيًا إلى المحاسبة عبر WhatsApp."
    ),
    "manual_booking.accounting_whatsapp_failed": "تعذر إرسال طلب رابط الدفع تلقائيًا إلى المحاسبة.",
    "manual_booking.cancelled": "تم إلغاء المسودة الداخلية قبل الدفع.",
    "manual_booking.deleted": "حُذفت المسودة نهائيًا قبل الدفع.",
}

_CANCELLATION_AUDIT_LABELS = {
    "cancellation.approved_locally": "تم اعتماد الإلغاء محليًا بانتظار التنفيذ الخارجي.",
    "cancellation.rejected_locally": "رُفض طلب الإلغاء محليًا.",
    "refund.amount_approved": "اعتمد مبلغ الاسترداد بعد المراجعة.",
    "refund.hyperpay_submitted": "أُرسل الاسترداد إلى HyperPay.",
    "refund.hyperpay_completed": "أكدت HyperPay الاسترداد.",
    "refund.hyperpay_failed": "رفضت HyperPay الاسترداد؛ لم يُسجل كتحويل.",
    "refund.hyperpay_review": "نتيجة الاسترداد غير مؤكدة وتحتاج مراجعة قبل أي إعادة محاولة.",
    "refund.marked_transferred": "سُجل تحويل الاسترداد للضيف.",
    "refund.cancelled_by_owner": "أغلق استحقاق الاسترداد دون تحويل.",
}


def _calendar_urls(form: ManualBookingAvailabilityForm) -> dict[str, str]:
    """Expose only public calendar endpoints for selectable active properties."""

    return {
        str(property_obj.pk): reverse(
            "reservations:calendar_availability",
            kwargs={"slug": property_obj.slug},
        )
        for property_obj in form.fields["property"].queryset.only("id", "slug")
    }


def _available_manual_properties(
    *,
    check_in: date,
    check_out: date,
) -> list[dict[str, str | int]]:
    """Return live, priced offers for the date-first owner flow.

    The owner begins by answering a guest's date enquiry, so every result must
    include Hostaway's current period total and the derived nightly average.
    Saving the selected offer still rechecks live availability and price before
    it creates a local draft.
    """

    available: list[dict[str, str | int]] = []
    properties = Property.objects.filter(
        is_visible=True,
        hostaway_is_active=True,
    ).order_by("city_ar", "name_ar", "name_en")
    with AvailabilityService() as service:
        for property_obj in properties:
            availability = service.check(
                AvailabilityRequest(
                    property=property_obj,
                    check_in=check_in,
                    check_out=check_out,
                    guests=1,
                ),
                bypass_cache=False,
            )
            quote = availability.quote
            if not availability.is_available or quote is None or quote.nights < 1:
                continue
            available.append(
                {
                    "id": str(property_obj.pk),
                    "name": str(property_obj),
                    "total_price": format(quote.total_price, "f"),
                    "currency": quote.currency,
                    "nights": quote.nights,
                    "average_nightly_price": format(
                        quote.total_price / Decimal(quote.nights), "f"
                    ),
                }
            )
    return available


@staff_member_required
def manual_booking_available_properties(request: HttpRequest) -> JsonResponse:
    """Owner-only endpoint for the date-first manual-booking property picker."""

    _require_owner(request)
    try:
        check_in = date.fromisoformat(request.GET["check_in"])
        check_out = date.fromisoformat(request.GET["check_out"])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"detail": "invalid_stay"}, status=400)
    if check_in < timezone.localdate() or check_out <= check_in:
        return JsonResponse({"detail": "invalid_stay"}, status=400)

    return JsonResponse(
        {
            "properties": _available_manual_properties(
                check_in=check_in,
                check_out=check_out,
            )
        }
    )


@staff_member_required
def manual_booking_list(request: HttpRequest) -> HttpResponse:
    _require_owner(request)
    ManualBookingDraft.objects.filter(
        status__in=(
            ManualBookingDraft.Status.QUOTED,
            ManualBookingDraft.Status.READY_FOR_PAYMENT,
        ),
        expires_at__lt=timezone.now(),
    ).update(status=ManualBookingDraft.Status.EXPIRED)

    status = request.GET.get("status", "")
    query = request.GET.get("q", "").strip()
    drafts = ManualBookingDraft.objects.select_related("property")
    if status in ManualBookingDraft.Status.values:
        drafts = drafts.filter(status=status)
    if query:
        drafts = drafts.filter(
            Q(guest_first_name__icontains=query)
            | Q(guest_last_name__icontains=query)
            | Q(guest_email__icontains=query)
            | Q(public_reference__icontains=query)
            | Q(property__name_ar__icontains=query)
            | Q(property__name_en__icontains=query)
            | Q(property__name_fr__icontains=query)
        )
    drafts = drafts.order_by("-created_at")[:50]
    return render(
        request,
        "admin/reservations/manual_booking_list.html",
        {
            "title": "مسودات الحجز اليدوي",
            "drafts": drafts,
            "status": status,
            "query": query,
            "status_options": ManualBookingDraft.Status.choices,
        },
    )


@staff_member_required
def booking_list(request: HttpRequest) -> HttpResponse:
    """The single owner workspace for paid, awaiting-payment, and cancelled stays."""

    _require_owner(request)
    query = request.GET.get("q", "").strip()
    successful_original_payment = PaymentAttempt.objects.filter(
        booking_intent_id=OuterRef("booking_intent_id"),
        modification_request__isnull=True,
        status=PaymentAttempt.Status.SUCCEEDED,
        verified_at__isnull=False,
    )
    bookings = (
        Reservation.objects.filter(booking_intent__isnull=False)
        .exclude(
            normalized_status__in=(
                Reservation.Status.CREATE_FAILED,
                Reservation.Status.CREATE_UNKNOWN,
                Reservation.Status.DECLINED,
                Reservation.Status.EXPIRED,
            )
        )
        .select_related("property", "booking_intent")
        .annotate(admin_has_paid=Exists(successful_original_payment))
        .order_by("-created_at")
    )
    if query:
        bookings = bookings.filter(
            Q(booking_intent__guest_first_name__icontains=query)
            | Q(booking_intent__guest_last_name__icontains=query)
            | Q(booking_intent__guest_email__icontains=query)
            | Q(booking_intent__guest_phone__icontains=query)
            | Q(public_reference__icontains=query)
            | Q(property__name_ar__icontains=query)
            | Q(property__name_en__icontains=query)
        )
    bookings = list(bookings[:100])
    for booking in bookings:
        payment_status = (booking.payment_status or "").strip().casefold()
        booking.admin_status_code = (
            "cancelled"
            if booking.normalized_status == Reservation.Status.CANCELLED
            else "completed"
            if booking.admin_has_paid or payment_status in {"paid", "partially_paid", "refunded"}
            else "awaiting_payment"
        )
        booking.admin_status_label = {
            "completed": "مكتمل ومدفوع",
            "awaiting_payment": "مؤكد بانتظار الدفع",
            "cancelled": "ملغى",
        }[booking.admin_status_code]
    return render(
        request,
        "admin/reservations/booking_list.html",
        {"title": "إدارة الحجوزات", "bookings": bookings, "query": query},
    )


@staff_member_required
def booking_detail(request: HttpRequest, reservation_id: str) -> HttpResponse:
    """One owner-only place to price, modify, or begin a cancellation."""

    _require_owner(request)
    reservation = get_object_or_404(
        Reservation.objects.select_related("property", "booking_intent"), pk=reservation_id
    )
    intent = reservation.booking_intent
    original_payment_amount = successful_original_payment_amount(reservation)
    has_successful_payment = original_payment_amount is not None and original_payment_amount > 0
    date_form = DateChangeRequestForm(
        initial={
            "new_check_in": reservation.check_in,
            "new_check_out": reservation.check_out,
            "new_guests": reservation.guests,
        }
    )
    pending_adjustments = list(reservation.modification_requests.exclude(
        request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION
    ).order_by("-requested_at")[:10])
    execution_form = None
    ready_adjustment = next(
        (
            item
            for item in pending_adjustments
            if item.status == BookingModificationRequest.Status.READY_FOR_HOSTAWAY
            and item.price_difference <= 0
        ),
        None,
    )
    if ready_adjustment is not None:
        execution_form = OwnerModificationExecutionForm(
            maximum_amount=(
                ready_adjustment.refund_amount
                if has_successful_payment
                else Decimal("0")
            )
        )
    increase_adjustment = next(
        (
            item
            for item in pending_adjustments
            if item.price_difference > 0
            and item.status
            in {
                BookingModificationRequest.Status.AWAITING_PAYMENT,
                BookingModificationRequest.Status.READY_FOR_HOSTAWAY,
            }
        ),
        None,
    )
    increase_delivery = (
        WhatsAppDelivery.objects.filter(modification_request=increase_adjustment).first()
        if increase_adjustment is not None
        else None
    )

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "quote_change":
            date_form = DateChangeRequestForm(request.POST)
            if date_form.is_valid() and intent is not None:
                with ModificationService() as service:
                    outcome = service.create_change_quote(
                        reservation,
                        new_check_in=date_form.cleaned_data["new_check_in"],
                        new_check_out=date_form.cleaned_data["new_check_out"],
                        new_guests=date_form.cleaned_data["new_guests"],
                        session_hash=f"owner:{request.user.pk}",
                        reason=date_form.cleaned_data.get("reason", ""),
                        owner_override=True,
                    )
                if outcome.request is not None:
                    record_audit(
                        request=request,
                        action="owner_booking.change_priced",
                        object_type="BookingModificationRequest",
                        object_reference=outcome.request.public_reference,
                        summary="Owner checked Hostaway availability and priced a booking change.",
                        metadata={"price_difference": format(outcome.request.price_difference, "f")},
                    )
                    if outcome.request.price_difference > 0:
                        messages.success(
                            request,
                            "تم فحص التوفر وتثبيت فرق السعر. رابط الدفع اليدوي سيُرسل للضيف "
                            "عند تفعيل واجهة HyperPay المخصصة للروابط.",
                        )
                    else:
                        messages.success(
                            request,
                            "تم فحص التوفر وتثبيت فرق الاسترداد. اختر مبلغ الاسترداد ثم أكّد التنفيذ.",
                        )
                    return redirect("notifications:booking_detail", reservation_id=reservation.pk)
                if outcome.code == "reservation_not_confirmed":
                    messages.error(
                        request,
                        "لا يمكن تعديل هذا الحجز الآن لأنه لم يُؤكد في Hostaway بعد. "
                        "يمكنك تعديله بعد أن تصبح حالته «مؤكد» فقط.",
                    )
                else:
                    messages.error(request, "تعذر تسعير التعديل الآن. لم يتغير الحجز.")
        elif action == "start_cancellation":
            if intent is None:
                messages.error(request, "لا توجد بيانات دفع مرتبطة بهذا الحجز.")
            else:
                with ModificationService() as service:
                    outcome = service.create_cancellation_request(
                        reservation,
                        session_hash=f"owner:{request.user.pk}",
                        reason=request.POST.get("reason", ""),
                        owner_override=True,
                    )
                if outcome.request is not None:
                    return redirect("notifications:cancellation_detail", request_id=outcome.request.pk)
                messages.error(request, f"تعذر تجهيز الإلغاء الآن ({outcome.code}).")
        elif action == "execute_adjustment":
            adjustment = get_object_or_404(
                BookingModificationRequest.objects.select_related("reservation"),
                pk=request.POST.get("modification_id"),
                reservation=reservation,
            )
            execution_form = OwnerModificationExecutionForm(
                request.POST,
                maximum_amount=(
                    adjustment.refund_amount if has_successful_payment else Decimal("0")
                ),
            )
            if execution_form.is_valid() and (
                adjustment.status == BookingModificationRequest.Status.READY_FOR_HOSTAWAY
                and adjustment.price_difference <= 0
            ):
                outcome = execute_automatic_modification(
                    adjustment,
                    approved_refund_amount=execution_form.cleaned_data["approved_refund_amount"],
                    refund_decision_note="",
                )
                if outcome.code == "completed":
                    messages.success(
                        request,
                        "أكدت Hostaway تعديل الحجز. أُرسل قرار الاسترداد إلى HyperPay عند وجود مبلغ.",
                    )
                    return redirect("notifications:booking_detail", reservation_id=reservation.pk)
                messages.error(request, f"لم يكتمل التعديل الخارجي ({outcome.code}).")
        elif action == "send_modification_payment_link_request":
            adjustment = get_object_or_404(
                BookingModificationRequest,
                pk=request.POST.get("modification_id"),
                reservation=reservation,
            )
            if adjustment.price_difference <= 0:
                messages.error(request, "لا يوجد فرق زيادة يحتاج إلى رابط دفع.")
            elif adjustment.status not in {
                BookingModificationRequest.Status.AWAITING_PAYMENT,
                BookingModificationRequest.Status.READY_FOR_HOSTAWAY,
            }:
                messages.error(request, "لم يعد طلب التعديل في حالة تسمح بطلب رابط دفع.")
            else:
                delivery_result = send_modification_payment_link_request(
                    modification_id=adjustment.pk,
                )
                if delivery_result.code == "sent":
                    record_audit(
                        request=request,
                        action="modification.accounting_whatsapp_sent",
                        object_type="BookingModificationRequest",
                        object_reference=adjustment.public_reference,
                        summary="The accounting payment-link request for a price increase was sent through UltraMsg.",
                        metadata={"price_difference": format(adjustment.price_difference, "f")},
                    )
                    messages.success(request, "أُرسل طلب إنشاء رابط فرق التعديل إلى أسيل عبر WhatsApp.")
                elif delivery_result.code == "already_sent":
                    messages.info(request, "سبق إرسال طلب رابط فرق التعديل إلى أسيل؛ لم تُرسل رسالة مكررة.")
                elif delivery_result.code == "already_requested":
                    messages.info(request, "طلب رابط فرق التعديل قيد الإرسال بالفعل إلى أسيل.")
                elif delivery_result.code == "previously_failed":
                    messages.error(request, "فشل طلب الرابط السابق؛ راجع سجل التسليم قبل أي متابعة.")
                else:
                    messages.error(request, "تعذر إرسال طلب رابط فرق التعديل إلى أسيل.")

    return render(
        request,
        "admin/reservations/booking_detail.html",
        {
            "title": "إدارة الحجز",
            "reservation": reservation,
            "date_form": date_form,
            "pending_adjustments": pending_adjustments,
            "ready_adjustment": ready_adjustment,
            "execution_form": execution_form,
            "increase_adjustment": increase_adjustment,
            "increase_delivery": increase_delivery,
            "has_successful_payment": has_successful_payment,
        },
    )


@staff_member_required
def manual_booking_create(request: HttpRequest) -> HttpResponse:
    _require_owner(request)
    if request.method == "POST":
        form = ManualBookingAvailabilityForm(request.POST)
        if form.is_valid():
            creation = create_manual_booking_draft(
                property_obj=form.cleaned_data["property"],
                check_in=form.cleaned_data["check_in"],
                check_out=form.cleaned_data["check_out"],
                guests=1,
                actor=request.user,
            )
            if creation.draft is not None:
                record_audit(
                    request=request,
                    action="manual_booking.availability_checked",
                    object_type="ManualBookingDraft",
                    object_reference=creation.draft.public_reference,
                    summary="Live availability and the Hostaway system price were captured.",
                    metadata={"source": "hostaway", "status": creation.draft.status},
                )
                messages.success(
                    request,
                    "تم التحقق من التوفر وحفظ سعر Hostaway. "
                    "أكمل بيانات الضيف واعتمد السعر النهائي.",
                )
                return redirect(
                    "notifications:manual_booking_detail",
                    draft_id=creation.draft.pk,
                )
            if creation.code == "currency_unavailable":
                form.add_error(
                    None,
                    "تعذّر تثبيت تحويل العملة لهذه المسودة. لم يُحفظ أي حجز.",
                )
            else:
                form.add_error(
                    None,
                    "الوحدة غير متاحة أو لا يمكن التحقق منها الآن. لم يُنشأ أي حجز.",
                )
    else:
        form = ManualBookingAvailabilityForm()
    return render(
        request,
        "admin/reservations/manual_booking_create.html",
        {
            "title": "حجز يدوي جديد",
            "form": form,
            "calendar_urls": _calendar_urls(form),
        },
    )


@staff_member_required
def manual_booking_detail(request: HttpRequest, draft_id: str) -> HttpResponse:
    _require_owner(request)
    draft = get_object_or_404(
        ManualBookingDraft.objects.select_related("property", "quote"),
        pk=draft_id,
    )
    form = ManualBookingFinalizeForm(draft=draft)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "recheck":
            result = recheck_manual_booking_draft(draft_id=draft.pk, actor=request.user)
            if result.code == "rechecked" and result.draft is not None:
                record_audit(
                    request=request,
                    action="manual_booking.rechecked",
                    object_type="ManualBookingDraft",
                    object_reference=result.draft.public_reference,
                    summary="Manual booking draft availability and price were rechecked.",
                    metadata={"source": "hostaway", "status": result.draft.status},
                )
                messages.success(
                    request,
                    "المسودة ما زالت متاحة. حُدّث السعر من Hostaway وأصبحت جاهزة للمراجعة من جديد.",
                )
                return redirect("notifications:manual_booking_detail", draft_id=draft.pk)
            if result.code == "unavailable":
                messages.error(
                    request,
                    "لم تعد هذه الوحدة متاحة بهذه التواريخ أو عدد الضيوف. لم يُنشأ أي حجز.",
                )
            elif result.code == "currency_unavailable":
                messages.error(request, "التوفر موجود لكن تعذر تثبيت السعر بالعملة المطلوبة الآن.")
            else:
                messages.error(request, "لا يمكن إعادة فحص هذه المسودة في حالتها الحالية.")
        elif action == "create_hostaway_and_request_payment":
            created = create_manual_booking_in_hostaway(draft_id=draft.pk)
            if created.code in {"created", "already_created"} and created.reservation is not None:
                if created.code == "created":
                    record_audit(
                        request=request,
                        action="manual_booking.hostaway_created",
                        object_type="ManualBookingDraft",
                        object_reference=draft.public_reference,
                        summary=(
                            "Owner-approved manual booking was created in Hostaway before payment."
                        ),
                        metadata={"status": "awaiting_payment"},
                    )
                try:
                    delivery_result = send_manual_payment_link_request(
                        reservation_id=created.reservation.pk,
                    )
                except Exception:
                    # Hostaway creation is authoritative and has already
                    # happened. Do not turn a delivery-side failure into a
                    # misleading 500 or encourage a duplicate booking retry.
                    logger.exception(
                        "Manual Hostaway booking %s was created but the accounting WhatsApp "
                        "request raised an unexpected error.",
                        created.reservation.pk,
                    )
                    record_audit(
                        request=request,
                        action="manual_booking.accounting_whatsapp_failed",
                        object_type="ManualBookingDraft",
                        object_reference=draft.public_reference,
                        summary="The accounting WhatsApp request raised an unexpected error.",
                        metadata={"status": "unexpected_error"},
                    )
                    messages.error(
                        request,
                        (
                            "تم إنشاء الحجز في Hostaway بانتظار الدفع، لكن تعذر إرسال طلب "
                            "رابط الدفع إلى أسيل. لم يُنشأ حجز مكرر؛ راجع سجل التسليم."
                        ),
                    )
                    return redirect(
                        "notifications:manual_booking_detail",
                        draft_id=created.draft.pk,
                    )
                if delivery_result.code == "sent":
                    record_audit(
                        request=request,
                        action="manual_booking.accounting_whatsapp_sent",
                        object_type="ManualBookingDraft",
                        object_reference=draft.public_reference,
                        summary="The accounting payment-link request was sent through UltraMsg.",
                        metadata={"status": "sent"},
                    )
                    messages.success(
                        request,
                        (
                            "تم إنشاء الحجز في Hostaway وهو بانتظار الدفع، وأُرسل طلب رابط "
                            "الدفع تلقائيًا إلى أسيل عبر WhatsApp."
                        ),
                    )
                elif delivery_result.code == "already_sent":
                    messages.info(
                        request,
                        (
                            "الحجز موجود في Hostaway بانتظار الدفع. سبق إرسال طلب رابط الدفع "
                            "إلى أسيل، ولذلك لم تُرسل رسالة مكررة."
                        ),
                    )
                elif delivery_result.code == "already_requested":
                    messages.info(
                        request,
                        "طلب رابط الدفع قيد الإرسال بالفعل إلى أسيل؛ لم تُرسل رسالة إضافية.",
                    )
                elif delivery_result.code == "previously_failed":
                    messages.error(
                        request,
                        "فشل طلب رابط الدفع السابق. لم يُعاد الإرسال تلقائيًا لتجنب تكرار الرسالة.",
                    )
                else:
                    record_audit(
                        request=request,
                        action="manual_booking.accounting_whatsapp_failed",
                        object_type="ManualBookingDraft",
                        object_reference=draft.public_reference,
                        summary="The automatic accounting payment-link request was not delivered.",
                        metadata={"status": delivery_result.code},
                    )
                    messages.error(
                        request,
                        (
                            "تم إنشاء الحجز في Hostaway، لكن تعذر إرسال طلب رابط الدفع تلقائيًا "
                            "إلى أسيل. راجع سجل التسليم قبل أي متابعة يدوية."
                        ),
                    )
                return redirect(
                    "notifications:manual_booking_detail",
                    draft_id=created.draft.pk,
                )
            if created.code == "quote_expired":
                messages.error(request, "انتهت صلاحية السعر. أعد فحص التوفر والسعر قبل إنشاء الحجز.")
            elif created.code == "availability_lost":
                messages.error(request, "لم تعد الوحدة متاحة. لم يُنشأ حجز في Hostaway.")
            else:
                messages.error(
                    request,
                    "تعذر إنشاء الحجز في Hostaway الآن. لم يُفتح طلب الدفع؛ أعد المحاولة بعد مراجعة التوفر.",
                )
        else:
            form = ManualBookingFinalizeForm(request.POST, draft=draft)
            if form.is_valid():
                finalized = finalize_manual_booking_draft(
                    draft_id=draft.pk,
                    guest_data=form.cleaned_data,
                    final_total_price=form.cleaned_data["final_total_price"],
                )
                if finalized.code == "ready_for_payment" and finalized.draft is not None:
                    record_audit(
                        request=request,
                        action="manual_booking.ready_for_payment",
                        object_type="ManualBookingDraft",
                        object_reference=finalized.draft.public_reference,
                        summary="Manual booking draft prepared for the later payment-link stage.",
                        metadata={
                            "source": finalized.draft.price_source,
                            "status": finalized.draft.status,
                        },
                    )
                    messages.success(
                        request,
                        "حُفظت المسودة وهي جاهزة للتأكيد في Hostaway ثم طلب رابط الدفع.",
                    )
                    return redirect(
                        "notifications:manual_booking_detail",
                        draft_id=finalized.draft.pk,
                    )
                if finalized.code == "quote_expired":
                    form.add_error(
                        None,
                        "انتهت صلاحية السعر. أعد فحص التوفر والسعر قبل المتابعة.",
                    )
                elif finalized.code == "currency_unavailable":
                    form.add_error(None, "تعذّر تثبيت تحويل العملة. لم تُحفظ التعديلات.")
                else:
                    form.add_error(None, "تعذّر حفظ المسودة. لم يُنفذ أي إجراء خارجي.")
    audit_entries = list(
        AuditLog.objects.filter(
        object_type="ManualBookingDraft",
        object_reference=draft.public_reference,
        )
        .select_related("actor_user")[:20]
    )
    for entry in audit_entries:
        entry.display_summary = _MANUAL_DRAFT_AUDIT_LABELS.get(entry.action, entry.summary)
    reservation = Reservation.objects.filter(booking_intent__quote=draft.quote).first()
    accounting_delivery = (
        WhatsAppDelivery.objects.filter(reservation=reservation).first()
        if reservation is not None
        else None
    )
    return render(
        request,
        "admin/reservations/manual_booking_detail.html",
        {
            "title": "مسودة حجز يدوي",
            "draft": draft,
            "form": form,
            "audit_entries": audit_entries,
            "accounting_whatsapp_name": settings.ACCOUNTING_WHATSAPP_NAME or "المحاسبة",
            "reservation": reservation,
            "accounting_delivery": accounting_delivery,
        },
    )


@staff_member_required
def manual_booking_disposal(request: HttpRequest, draft_id: str) -> HttpResponse:
    """Cancel or remove a local manual draft from the cancellation workspace."""

    _require_owner(request)
    draft = get_object_or_404(
        ManualBookingDraft.objects.select_related("property", "quote"),
        pk=draft_id,
    )
    reservation = Reservation.objects.filter(booking_intent__quote=draft.quote).first()
    if draft.status == ManualBookingDraft.Status.BOOKED_AWAITING_PAYMENT:
        if reservation is not None:
            messages.info(request, "أصبحت هذه المسودة حجزًا في Hostaway. أدره من صفحة الحجز.")
            return redirect("notifications:booking_detail", reservation_id=reservation.pk)
        messages.error(request, "هذه المسودة مرتبطة بحجز مؤكد، ولا يمكن حذفها من هنا.")
        return redirect("notifications:booking_list")
    cancel_form = ManualBookingCancelForm()
    delete_form = ManualBookingDeleteForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "cancel":
            cancel_form = ManualBookingCancelForm(request.POST)
            if cancel_form.is_valid():
                if draft.status not in {
                    ManualBookingDraft.Status.QUOTED,
                    ManualBookingDraft.Status.READY_FOR_PAYMENT,
                }:
                    messages.error(request, "لا يمكن إلغاء هذه المسودة في حالتها الحالية.")
                else:
                    draft.status = ManualBookingDraft.Status.CANCELLED
                    draft.save(update_fields=["status", "updated_at"])
                    record_audit(
                        request=request,
                        action="manual_booking.cancelled",
                        object_type="ManualBookingDraft",
                        object_reference=draft.public_reference,
                        summary="Manual booking draft cancelled before payment.",
                        metadata={"status": draft.status},
                    )
                    messages.success(
                        request,
                        "أُلغيت المسودة الداخلية. لم يُلغَ حجز في Hostaway ولم يُنفذ أي استرجاع.",
                    )
                    return redirect("notifications:cancellation_list")
        elif action == "delete":
            delete_form = ManualBookingDeleteForm(request.POST)
            if delete_form.is_valid():
                public_reference = draft.public_reference
                quote_id = draft.quote_id
                with transaction.atomic():
                    record_audit(
                        request=request,
                        action="manual_booking.deleted",
                        object_type="ManualBookingDraft",
                        object_reference=public_reference,
                        summary="Manual booking draft permanently deleted before payment.",
                        metadata={"status": draft.status},
                    )
                    draft.delete()
                    BookingQuote.objects.filter(
                        pk=quote_id,
                        booking_intent__isnull=True,
                    ).delete()
                messages.success(
                    request,
                    "حُذفت المسودة نهائيًا من القائمة. بقي سجل تدقيق مختصر لحماية المتابعة.",
                )
                return redirect("notifications:cancellation_list")

    return render(
        request,
        "admin/reservations/manual_booking_disposal.html",
        {
            "title": "إلغاء أو حذف مسودة",
            "draft": draft,
            "cancel_form": cancel_form,
            "delete_form": delete_form,
        },
    )


@staff_member_required
def cancellation_list(request: HttpRequest) -> HttpResponse:
    """Keep the legacy URL inside the one reservations workspace."""

    _require_owner(request)
    return booking_list(request)


@staff_member_required
def cancellation_detail(request: HttpRequest, request_id: str) -> HttpResponse:
    """Review one cancellation before any separately-enabled Hostaway action."""

    _require_owner(request)
    cancellation = get_object_or_404(
        BookingModificationRequest.objects.select_related(
            "reservation__property", "reservation__booking_intent"
        ).prefetch_related("refund_obligations"),
        pk=request_id,
        request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION,
    )
    approval_form = CancellationDecisionForm()
    rejection_form = CancellationRejectionForm()
    original_payment_amount = successful_original_payment_amount(cancellation.reservation)
    has_successful_payment = original_payment_amount is not None and original_payment_amount > 0
    estimated_refund = cancellation_refund(cancellation.reservation)
    execution_form = CancellationExecutionForm(maximum_amount=estimated_refund.amount)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "approve":
            approval_form = CancellationDecisionForm(request.POST)
            if approval_form.is_valid():
                if cancellation.status != BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL:
                    messages.error(request, "لا يمكن اعتماد هذا الطلب في حالته الحالية.")
                else:
                    now = timezone.now()
                    cancellation.status = BookingModificationRequest.Status.READY_FOR_HOSTAWAY
                    cancellation.approved_at = now
                    cancellation.save(update_fields=["status", "approved_at", "updated_at"])
                    record_audit(
                        request=request,
                        action="cancellation.approved_locally",
                        object_type="BookingModificationRequest",
                        object_reference=cancellation.public_reference,
                        summary="Cancellation approved locally; no Hostaway request was sent.",
                        metadata={"note": approval_form.cleaned_data["decision_note"]},
                    )
                    messages.success(
                        request,
                        "اعتمد الإلغاء داخل النظام فقط. "
                        "لم يُرسل أي إلغاء إلى Hostaway ولم يُنفذ استرداد.",
                    )
                    return redirect("notifications:cancellation_detail", request_id=cancellation.pk)
        elif action == "reject":
            rejection_form = CancellationRejectionForm(request.POST)
            if rejection_form.is_valid():
                if cancellation.status in {
                    BookingModificationRequest.Status.COMPLETED,
                    BookingModificationRequest.Status.REJECTED,
                }:
                    messages.error(request, "لا يمكن رفض طلب مكتمل أو مرفوض مسبقًا.")
                else:
                    now = timezone.now()
                    cancellation.status = BookingModificationRequest.Status.REJECTED
                    cancellation.rejected_at = now
                    cancellation.save(update_fields=["status", "rejected_at", "updated_at"])
                    record_audit(
                        request=request,
                        action="cancellation.rejected_locally",
                        object_type="BookingModificationRequest",
                        object_reference=cancellation.public_reference,
                        summary="Cancellation rejected locally.",
                        metadata={"note": rejection_form.cleaned_data["decision_note"]},
                    )
                    messages.success(request, "رُفض الطلب محليًا مع حفظ سبب القرار في سجل التدقيق.")
                    return redirect("notifications:cancellation_detail", request_id=cancellation.pk)
        elif action == "execute_external":
            execution_form = CancellationExecutionForm(
                request.POST,
                maximum_amount=estimated_refund.amount,
            )
            if execution_form.is_valid():
                if cancellation.status != BookingModificationRequest.Status.READY_FOR_HOSTAWAY:
                    messages.error(request, "لا يمكن تنفيذ الإلغاء الخارجي في حالته الحالية.")
                else:
                    outcome = execute_automatic_modification(
                        cancellation,
                        approved_refund_amount=execution_form.cleaned_data[
                            "approved_refund_amount"
                        ],
                        refund_decision_note="",
                    )
                    if outcome.code == "completed":
                        record_audit(
                            request=request,
                            action="cancellation.executed_hostaway",
                            object_type="BookingModificationRequest",
                            object_reference=cancellation.public_reference,
                            summary="Hostaway confirmed the cancellation.",
                            metadata={
                                "approved_refund": format(
                                    execution_form.cleaned_data["approved_refund_amount"], "f"
                                ),
                                "refund_result": outcome.refund_code,
                            },
                        )
                        if outcome.refund_code == "refund.hyperpay_completed":
                            messages.success(
                                request,
                                "أكدت Hostaway الإلغاء، وأكدت HyperPay إعادة المبلغ "
                                "إلى بطاقة الضيف.",
                            )
                        elif outcome.refund_code == "refund.hyperpay_submitted":
                            messages.success(
                                request,
                                "أكدت Hostaway الإلغاء، وأُرسل الاسترداد إلى HyperPay لبطاقة الضيف.",
                            )
                        elif outcome.refund is not None:
                            messages.warning(
                                request,
                                "أكدت Hostaway الإلغاء، لكن الاسترداد يحتاج مراجعة "
                                "قبل إعادة المحاولة.",
                            )
                        else:
                            messages.success(request, "أكدت Hostaway إلغاء الحجز.")
                    else:
                        messages.error(request, f"لم يكتمل الإلغاء الخارجي ({outcome.code}).")
                    return redirect("notifications:cancellation_detail", request_id=cancellation.pk)

    audit_entries = list(
        AuditLog.objects.filter(
            object_type="BookingModificationRequest",
            object_reference=cancellation.public_reference,
        ).select_related("actor_user")[:20]
    )
    for entry in audit_entries:
        entry.display_summary = _CANCELLATION_AUDIT_LABELS.get(entry.action, entry.summary)
    return render(
        request,
        "admin/reservations/cancellation_detail.html",
        {
            "title": "مراجعة طلب الإلغاء",
            "cancellation": cancellation,
            "estimated_refund": estimated_refund,
            "has_successful_payment": has_successful_payment,
            "original_payment_amount": original_payment_amount,
            "refunds": cancellation.refund_obligations.all(),
            "approval_form": approval_form,
            "rejection_form": rejection_form,
            "execution_form": execution_form,
            "audit_entries": audit_entries,
        },
    )


@staff_member_required
def refund_list(request: HttpRequest) -> HttpResponse:
    """A local record of money owed; opening it never moves funds."""

    _require_owner(request)
    status = request.GET.get("status", "")
    query = request.GET.get("q", "").strip()
    refunds = RefundObligation.objects.select_related(
        "reservation__property", "reservation__booking_intent", "modification_request"
    )
    if status in RefundObligation.Status.values:
        refunds = refunds.filter(status=status)
    if query:
        refunds = refunds.filter(
            Q(reservation__booking_intent__guest_first_name__icontains=query)
            | Q(reservation__booking_intent__guest_last_name__icontains=query)
            | Q(reservation__booking_intent__guest_email__icontains=query)
            | Q(public_reference__icontains=query)
            | Q(reservation__public_reference__icontains=query)
        )
    return render(
        request,
        "admin/reservations/refund_list.html",
        {
            "title": "الاستردادات المستحقة",
            "refunds": refunds.order_by("-created_at")[:100],
            "status": status,
            "query": query,
            "status_options": RefundObligation.Status.choices,
        },
    )


@staff_member_required
def refund_detail(request: HttpRequest, refund_id: str) -> HttpResponse:
    """Approve a full/partial amount and record an already-made transfer safely."""

    _require_owner(request)
    refund = get_object_or_404(
        RefundObligation.objects.select_related(
            "reservation__property", "reservation__booking_intent", "modification_request"
        ),
        pk=refund_id,
    )
    decision_form = RefundDecisionForm(current_amount=refund.amount)
    settlement_form = RefundSettlementForm()
    gateway_form = RefundGatewaySubmitForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "approve_amount":
            decision_form = RefundDecisionForm(request.POST, current_amount=refund.amount)
            if decision_form.is_valid():
                approved_amount = decision_form.cleaned_data["approved_amount"]
                if refund.status != RefundObligation.Status.DUE:
                    messages.error(request, "لا يمكن تغيير مبلغ استرداد تم إقفاله أو تحويله.")
                else:
                    original_amount = refund.amount
                    decision_note = decision_form.cleaned_data["decision_note"]
                    calculation = (
                        deepcopy(refund.calculation) if isinstance(refund.calculation, dict) else {}
                    )
                    calculation["operator_decision"] = {
                        "original_amount": format(original_amount, "f"),
                        "approved_amount": format(approved_amount, "f"),
                        "note": decision_note,
                        "decided_at": timezone.now().isoformat(),
                    }
                    refund.amount = approved_amount
                    refund.note = decision_note
                    refund.calculation = calculation
                    refund.save(update_fields=["amount", "note", "calculation", "updated_at"])
                    record_audit(
                        request=request,
                        action="refund.amount_approved",
                        object_type="RefundObligation",
                        object_reference=refund.public_reference,
                        summary="Refund amount approved locally; no financial transfer was made.",
                        metadata={
                            "original_amount": format(original_amount, "f"),
                            "approved_amount": format(approved_amount, "f"),
                            "currency": refund.currency,
                        },
                    )
                    messages.success(
                        request,
                        "حُفظ مبلغ الاسترداد المعتمد. لا يزال التحويل المالي خطوة مستقلة.",
                    )
                    return redirect("notifications:refund_detail", refund_id=refund.pk)
        elif action == "mark_transferred":
            settlement_form = RefundSettlementForm(request.POST)
            if settlement_form.is_valid():
                if refund.status != RefundObligation.Status.DUE:
                    messages.error(request, "لا يمكن تسجيل تحويل لهذا الاسترداد في حالته الحالية.")
                elif refund.amount <= Decimal("0"):
                    messages.error(request, "لا يوجد مبلغ لتحويله. أغلق الاستحقاق بدلًا من ذلك.")
                else:
                    now = timezone.now()
                    refund.status = RefundObligation.Status.TRANSFERRED
                    refund.transfer_reference = settlement_form.cleaned_data["transfer_reference"]
                    refund.note = settlement_form.cleaned_data["settlement_note"] or refund.note
                    refund.transferred_at = now
                    refund.transferred_by = request.user
                    refund.save(
                        update_fields=[
                            "status",
                            "transfer_reference",
                            "note",
                            "transferred_at",
                            "transferred_by",
                            "updated_at",
                        ]
                    )
                    record_audit(
                        request=request,
                        action="refund.marked_transferred",
                        object_type="RefundObligation",
                        object_reference=refund.public_reference,
                        summary="Refund was recorded as transferred after owner confirmation.",
                        metadata={
                            "amount": format(refund.amount, "f"),
                            "currency": refund.currency,
                        },
                    )
                    messages.success(
                        request,
                        "سُجل التحويل في النظام. لم يتصل الموقع بأي بوابة دفع.",
                    )
                    return redirect("notifications:refund_detail", refund_id=refund.pk)
        elif action == "submit_hyperpay_refund":
            gateway_form = RefundGatewaySubmitForm(request.POST)
            if gateway_form.is_valid():
                from apps.payments.hyperpay.exceptions import HyperPayRefundError

                try:
                    outcome = HyperPayRefundService().submit(refund, operator=request.user)
                except HyperPayRefundError as exc:
                    messages.error(request, f"تعذر إرسال الاسترداد إلى HyperPay ({exc.code}).")
                else:
                    record_audit(
                        request=request,
                        action=outcome.audit_action,
                        object_type="RefundObligation",
                        object_reference=refund.public_reference,
                        summary=outcome.audit_summary,
                        metadata=outcome.audit_metadata,
                    )
                    messages.success(request, outcome.message)
                    return redirect("notifications:refund_detail", refund_id=refund.pk)
        elif action == "close_without_transfer":
            if refund.status != RefundObligation.Status.DUE:
                messages.error(request, "لا يمكن إغلاق هذا الاستحقاق في حالته الحالية.")
            elif refund.amount != Decimal("0"):
                messages.error(
                    request,
                    "اعتمد مبلغ 0 أولًا مع سبب واضح قبل إغلاق الاستحقاق دون تحويل.",
                )
            else:
                refund.status = RefundObligation.Status.CANCELLED
                refund.save(update_fields=["status", "updated_at"])
                record_audit(
                    request=request,
                    action="refund.cancelled_by_owner",
                    object_type="RefundObligation",
                    object_reference=refund.public_reference,
                    summary="Zero-value refund obligation closed by owner.",
                    metadata={},
                )
                messages.success(request, "أُغلق الاستحقاق الصفري مع بقاء سجل القرار محفوظًا.")
                return redirect("notifications:refund_detail", refund_id=refund.pk)

    audit_entries = list(
        AuditLog.objects.filter(
            object_type="RefundObligation", object_reference=refund.public_reference
        ).select_related("actor_user")[:20]
    )
    for entry in audit_entries:
        entry.display_summary = _CANCELLATION_AUDIT_LABELS.get(entry.action, entry.summary)
    return render(
        request,
        "admin/reservations/refund_detail.html",
        {
            "title": "مراجعة الاسترداد",
            "refund": refund,
            "decision_form": decision_form,
            "settlement_form": settlement_form,
            "gateway_form": gateway_form,
            "hyperpay_refund_available": HyperPayRefundService.is_available(),
            "audit_entries": audit_entries,
        },
    )
