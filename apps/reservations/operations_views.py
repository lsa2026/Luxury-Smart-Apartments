"""Owner-only operations screens for preparing manual booking drafts."""

from __future__ import annotations

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

from .manual_bookings import create_manual_booking_draft, finalize_manual_booking_draft
from .models import BookingQuote, ManualBookingDraft
from .operations_forms import (
    ManualBookingAvailabilityForm,
    ManualBookingCancelForm,
    ManualBookingDeleteForm,
    ManualBookingFinalizeForm,
)


def _require_owner(request: HttpRequest) -> None:
    require_operations_owner(request.user)


_MANUAL_DRAFT_AUDIT_LABELS = {
    "manual_booking.availability_checked": "تم فحص التوفر وتثبيت سعر النظام.",
    "manual_booking.ready_for_payment": "تم اعتماد بيانات الضيف والسعر النهائي.",
    "manual_booking.cancelled": "تم إلغاء المسودة الداخلية قبل الدفع.",
    "manual_booking.deleted": "حُذفت المسودة نهائيًا قبل الدفع.",
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
