"""Owner-only operations screens for preparing manual booking drafts."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.access import require_operations_owner
from apps.notifications.models import AuditLog
from apps.notifications.services.audit import record_audit
from apps.payments.hyperpay.refunds import HyperPayRefundService

from .manual_bookings import create_manual_booking_draft, finalize_manual_booking_draft
from .models import (
    BookingModificationRequest,
    BookingQuote,
    ManualBookingDraft,
    RefundObligation,
)
from .operations_forms import (
    CancellationDecisionForm,
    CancellationExecutionForm,
    CancellationRejectionForm,
    ManualBookingAvailabilityForm,
    ManualBookingCancelForm,
    ManualBookingDeleteForm,
    ManualBookingFinalizeForm,
    RefundDecisionForm,
    RefundGatewaySubmitForm,
    RefundSettlementForm,
)
from .services.automatic_modifications import execute_automatic_modification
from .services.refunds import cancellation_refund


def _require_owner(request: HttpRequest) -> None:
    require_operations_owner(request.user)


_MANUAL_DRAFT_AUDIT_LABELS = {
    "manual_booking.availability_checked": "تم فحص التوفر وتثبيت سعر النظام.",
    "manual_booking.ready_for_payment": "تم اعتماد بيانات الضيف والسعر النهائي.",
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
def manual_booking_create(request: HttpRequest) -> HttpResponse:
    _require_owner(request)
    if request.method == "POST":
        form = ManualBookingAvailabilityForm(request.POST)
        if form.is_valid():
            creation = create_manual_booking_draft(
                property_obj=form.cleaned_data["property"],
                check_in=form.cleaned_data["check_in"],
                check_out=form.cleaned_data["check_out"],
                guests=form.cleaned_data["guests"],
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
    cancel_form = ManualBookingCancelForm()
    delete_form = ManualBookingDeleteForm()
    form = ManualBookingFinalizeForm(draft=draft)
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
                    return redirect("notifications:manual_booking_detail", draft_id=draft.pk)
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
                return redirect("notifications:manual_booking_list")
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
                        "حُفظت المسودة. لم يُرسل رابط دفع ولم يُنشأ حجز في Hostaway بعد.",
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
    return render(
        request,
        "admin/reservations/manual_booking_detail.html",
        {
            "title": "مسودة حجز يدوي",
            "draft": draft,
            "form": form,
            "cancel_form": cancel_form,
            "delete_form": delete_form,
            "audit_entries": audit_entries,
        },
    )


@staff_member_required
def cancellation_list(request: HttpRequest) -> HttpResponse:
    """Owner queue for cancellation decisions, with no provider call on GET."""

    _require_owner(request)
    status = request.GET.get("status", "")
    query = request.GET.get("q", "").strip()
    cancellations = BookingModificationRequest.objects.filter(
        request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION
    ).select_related("reservation__property", "reservation__booking_intent")
    if status in BookingModificationRequest.Status.values:
        cancellations = cancellations.filter(status=status)
    if query:
        cancellations = cancellations.filter(
            Q(reservation__booking_intent__guest_first_name__icontains=query)
            | Q(reservation__booking_intent__guest_last_name__icontains=query)
            | Q(reservation__booking_intent__guest_email__icontains=query)
            | Q(public_reference__icontains=query)
            | Q(reservation__public_reference__icontains=query)
        )
    return render(
        request,
        "admin/reservations/cancellation_list.html",
        {
            "title": "طلبات الإلغاء والاسترداد",
            "cancellations": cancellations.order_by("-requested_at")[:100],
            "status": status,
            "query": query,
            "status_options": BookingModificationRequest.Status.choices,
        },
    )


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
    execution_form = CancellationExecutionForm()
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
            execution_form = CancellationExecutionForm(request.POST)
            if execution_form.is_valid():
                if cancellation.status != BookingModificationRequest.Status.READY_FOR_HOSTAWAY:
                    messages.error(request, "لا يمكن تنفيذ الإلغاء الخارجي في حالته الحالية.")
                else:
                    outcome = execute_automatic_modification(cancellation)
                    if outcome.code == "completed":
                        record_audit(
                            request=request,
                            action="cancellation.executed_hostaway",
                            object_type="BookingModificationRequest",
                            object_reference=cancellation.public_reference,
                            summary="Hostaway confirmed the cancellation.",
                            metadata={
                                "estimated_refund": format(
                                    cancellation_refund(cancellation.reservation).amount,
                                    "f",
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

    estimated_refund = cancellation_refund(cancellation.reservation)
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
