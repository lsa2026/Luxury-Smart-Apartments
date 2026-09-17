"""Owner-only operations screens for preparing manual booking drafts."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.access import require_operations_owner
from apps.notifications.services.audit import record_audit

from .manual_bookings import create_manual_booking_draft, finalize_manual_booking_draft
from .models import ManualBookingDraft
from .operations_forms import ManualBookingAvailabilityForm, ManualBookingFinalizeForm


def _require_owner(request: HttpRequest) -> None:
    require_operations_owner(request.user)


@staff_member_required
def manual_booking_list(request: HttpRequest) -> HttpResponse:
    _require_owner(request)
    drafts = ManualBookingDraft.objects.select_related("property").order_by("-created_at")[:50]
    return render(
        request,
        "admin/reservations/manual_booking_list.html",
        {"title": "مسودات الحجز اليدوي", "drafts": drafts},
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
        {"title": "حجز يدوي جديد", "form": form},
    )


@staff_member_required
def manual_booking_detail(request: HttpRequest, draft_id: str) -> HttpResponse:
    _require_owner(request)
    draft = get_object_or_404(
        ManualBookingDraft.objects.select_related("property", "quote"),
        pk=draft_id,
    )
    if request.method == "POST":
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
    else:
        form = ManualBookingFinalizeForm(draft=draft)
    return render(
        request,
        "admin/reservations/manual_booking_detail.html",
        {"title": "مسودة حجز يدوي", "draft": draft, "form": form},
    )
