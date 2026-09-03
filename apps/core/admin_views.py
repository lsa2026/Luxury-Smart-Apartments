"""Purpose-built staff views that combine related operational records."""

from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _

from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingIntent


@staff_member_required
def customer_overview(request: HttpRequest) -> HttpResponse:
    if not (
        request.user.is_superuser
        or request.user.has_perm("reservations.view_bookingintent")
    ):
        raise PermissionDenied

    queryset = (
        BookingIntent.objects.select_related("property", "reservation")
        .prefetch_related("payment_attempts")
        .order_by("-created_at")
    )
    query = request.GET.get("q", "").strip()
    booking_status = request.GET.get("status", "").strip()
    payment_status = request.GET.get("payment", "").strip()
    can_view_pii = bool(
        request.user.is_superuser
        or request.user.has_perm("reservations.view_bookingintent_pii")
    )
    if query:
        filters = Q(public_reference__icontains=query) | Q(
            property__name_ar__icontains=query
        ) | Q(property__name_en__icontains=query)
        if can_view_pii:
            filters |= (
                Q(guest_first_name__icontains=query)
                | Q(guest_last_name__icontains=query)
                | Q(guest_email__icontains=query)
                | Q(guest_phone__icontains=query)
            )
        queryset = queryset.filter(filters)
    if booking_status in BookingIntent.Status.values:
        queryset = queryset.filter(status=booking_status)
    if payment_status in PaymentAttempt.Status.values:
        queryset = queryset.filter(payment_attempts__status=payment_status).distinct()

    page = Paginator(queryset, 20).get_page(request.GET.get("page"))
    rows = []
    for intent in page.object_list:
        payments = list(intent.payment_attempts.all())
        payment = payments[0] if payments else None
        reservation = getattr(intent, "reservation", None)
        rows.append(
            {
                "intent": intent,
                "reservation": reservation,
                "payment": payment,
                "guest_name": (
                    f"{intent.guest_first_name} {intent.guest_last_name}".strip()
                    if can_view_pii
                    else _("Protected data")
                ),
                "guest_email": intent.guest_email if can_view_pii else "••••••••",
                "guest_phone": intent.guest_phone if can_view_pii else "••••••••",
            }
        )
    page.object_list = rows
    return render(
        request,
        "admin/core/customers.html",
        {
            "title": _("Customers and bookings"),
            "page_obj": page,
            "query": query,
            "selected_status": booking_status,
            "selected_payment": payment_status,
            "booking_statuses": BookingIntent.Status.choices,
            "payment_statuses": PaymentAttempt.Status.choices,
            "can_view_pii": can_view_pii,
        },
    )
