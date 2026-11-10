"""Private stored guest files. Opening one never repeats a completed provider scan."""

from django.contrib import admin
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import translation

from apps.accounts.access import operations_access_required

from .models import AdminGuestAnchor, AdminGuestFile, Reservation
from .services.guest_classification_history import ensure_refresh, provider_state
from .services.guest_files import history_for_display


@operations_access_required
@translation.override("ar")
def guest_file_for_booking(request, reservation_id):
    booking = get_object_or_404(
        Reservation.objects.select_related("booking_intent"), pk=reservation_id, is_test=False
    )
    if request.method == "POST":
        ensure_refresh(force=True)
        return redirect("notifications:guest_file_for_booking", reservation_id=booking.pk)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    anchor = AdminGuestAnchor.objects.filter(
        hostaway_reservation_id=booking.hostaway_reservation_id
    ).first()
    if anchor:
        return redirect("notifications:guest_file", file_id=anchor.guest_file_id)
    state = ensure_refresh()
    return render(
        request,
        "admin/reservations/guest_file.html",
        {
            **admin.site.each_context(request),
            "title": "ملف الضيف",
            "booking": booking,
            "state": state,
            "status_url": reverse("notifications:booking_list") + "?classification_status=1",
        },
    )


@operations_access_required
@translation.override("ar")
def guest_file(request, file_id):
    file = get_object_or_404(AdminGuestFile, pk=file_id)
    if request.method == "POST":
        if request.POST.get("action") == "refresh":
            ensure_refresh(force=True)
        return redirect("notifications:guest_file", file_id=file.pk)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    anchors = list(file.anchors.values_list("hostaway_reservation_id", flat=True))
    booking = (
        Reservation.objects.filter(hostaway_reservation_id__in=anchors, is_test=False)
        .select_related("booking_intent")
        .order_by("-check_in")
        .first()
    )
    return render(
        request,
        "admin/reservations/guest_file.html",
        {
            **admin.site.each_context(request),
            "title": "ملف الضيف",
            "file": file,
            "history": history_for_display(file),
            "booking": booking,
            "state": provider_state(),
            "status_url": reverse("notifications:booking_list") + "?classification_status=1",
            "classification_label": {
                "new": "ضيف جديد",
                "returning": "ضيف عائد",
                "unknown": "غير متحقق",
            }.get(file.classification, "غير متحقق"),
        },
    )
