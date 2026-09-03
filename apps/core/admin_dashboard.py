"""Read-only aggregates used by the bespoke administration experience."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from apps.core.models import ContactMessage, SitePage
from apps.integrations.models import HostawayWebhookEvent, IntegrationSyncRun
from apps.notifications.models import EmailDelivery, Notification
from apps.payments.models import PaymentAttempt
from apps.properties.models import Property
from apps.reservations.models import BookingIntent, BookingModificationRequest, Reservation


def _url(name: str, *args: object) -> str:
    return reverse(name, args=args)


def _trend_rows() -> list[dict[str, Any]]:
    today = timezone.localdate()
    start = today - timedelta(days=6)
    counts = {
        item["day"]: item["total"]
        for item in BookingIntent.objects.filter(created_at__date__gte=start)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Count("id"))
        .order_by("day")
    }
    rows = [
        {
            "date": start + timedelta(days=offset),
            "label": (start + timedelta(days=offset)).strftime("%d/%m"),
            "total": counts.get(start + timedelta(days=offset), 0),
        }
        for offset in range(7)
    ]
    maximum = max((row["total"] for row in rows), default=0) or 1
    for row in rows:
        row["height"] = max(8, round((row["total"] / maximum) * 100))
    return rows


def dashboard_payload(request: object) -> dict[str, Any]:
    """Return permission-aware local metrics without contacting third parties."""
    today = timezone.localdate()
    now = timezone.now()
    month_start = today.replace(day=1)
    rolling_start = now - timedelta(days=30)
    week_end = today + timedelta(days=6)
    revenue_by_currency = list(
        PaymentAttempt.objects.filter(
            status=PaymentAttempt.Status.SUCCEEDED,
            created_at__date__gte=month_start,
        )
        .values("currency")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )
    revenue = (
        revenue_by_currency[0]
        if revenue_by_currency
        else {
            "total": Decimal("0"),
            "currency": "SAR",
        }
    )
    latest_sync = IntegrationSyncRun.objects.order_by("-started_at").first()
    recent_intents = list(
        BookingIntent.objects.select_related("property", "reservation")
        .prefetch_related("payment_attempts")
        .order_by("-created_at")[:6]
    )
    can_view_pii = bool(
        request.user.is_superuser or request.user.has_perm("reservations.view_bookingintent_pii")
    )
    recent_rows = []
    for intent in recent_intents:
        payments = list(intent.payment_attempts.all())
        payment = payments[0] if payments else None
        reservation = getattr(intent, "reservation", None)
        recent_rows.append(
            {
                "reference": intent.public_reference,
                "guest": (
                    f"{intent.guest_first_name} {intent.guest_last_name}".strip()
                    if can_view_pii
                    else "بيانات عميل محمية"
                ),
                "property": str(intent.property),
                "date": intent.created_at,
                "total": intent.total_price,
                "currency": intent.currency,
                "status": (
                    reservation.get_normalized_status_display()
                    if reservation
                    else intent.get_status_display()
                ),
                "status_key": reservation.normalized_status if reservation else intent.status,
                "payment": payment.get_status_display() if payment else "لم يبدأ",
                "payment_key": payment.status if payment else "none",
                "url": _url("admin:reservations_bookingintent_change", intent.pk),
            }
        )

    active_reservation_statuses = (
        Reservation.Status.CONFIRMED,
        Reservation.Status.MODIFIED,
    )
    upcoming_reservations = list(
        Reservation.objects.filter(
            Q(check_in__range=(today, week_end)) | Q(check_out__range=(today, week_end)),
            normalized_status__in=active_reservation_statuses,
        )
        .select_related("property", "booking_intent")
        .order_by("check_in", "check_out")
    )
    schedule_rows: list[dict[str, Any]] = []
    for reservation in upcoming_reservations:
        intent = reservation.booking_intent
        guest = "بيانات عميل محمية"
        if can_view_pii and intent:
            guest = f"{intent.guest_first_name} {intent.guest_last_name}".strip()
        property_name = str(reservation.property) if reservation.property else "وحدة غير مرتبطة"
        if today <= reservation.check_in <= week_end:
            schedule_rows.append(
                {
                    "kind": "arrival",
                    "kind_label": "وصول",
                    "date": reservation.check_in,
                    "guest": guest,
                    "property": property_name,
                    "reference": reservation.public_reference,
                    "guests": reservation.guests,
                    "url": _url("admin:reservations_reservation_change", reservation.pk),
                }
            )
        if today <= reservation.check_out <= week_end:
            schedule_rows.append(
                {
                    "kind": "departure",
                    "kind_label": "مغادرة",
                    "date": reservation.check_out,
                    "guest": guest,
                    "property": property_name,
                    "reference": reservation.public_reference,
                    "guests": reservation.guests,
                    "url": _url("admin:reservations_reservation_change", reservation.pk),
                }
            )
    schedule_rows.sort(key=lambda row: (row["date"], row["kind"] != "arrival"))

    pending_payment_statuses = (
        PaymentAttempt.Status.CREATED,
        PaymentAttempt.Status.PENDING,
    )
    payment_attention_statuses = (
        PaymentAttempt.Status.FAILED,
        PaymentAttempt.Status.REVIEW,
        PaymentAttempt.Status.UNKNOWN,
    )
    reservation_attention_statuses = (
        Reservation.Status.CREATE_FAILED,
        Reservation.Status.CREATE_UNKNOWN,
        Reservation.Status.SYNC_PENDING,
        Reservation.Status.UNKNOWN,
    )
    pending_modification_statuses = (
        BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL,
        BookingModificationRequest.Status.AWAITING_PAYMENT,
        BookingModificationRequest.Status.READY_FOR_HOSTAWAY,
        BookingModificationRequest.Status.UNKNOWN,
    )

    pending_payments = PaymentAttempt.objects.filter(status__in=pending_payment_statuses).count()
    stale_pending_payments = PaymentAttempt.objects.filter(
        status__in=pending_payment_statuses,
        updated_at__lt=now - timedelta(minutes=30),
    ).count()
    payment_attention = PaymentAttempt.objects.filter(
        status__in=payment_attention_statuses,
        created_at__gte=rolling_start,
    ).count()
    reservations_attention = Reservation.objects.filter(
        normalized_status__in=reservation_attention_statuses
    ).count()
    pending_modifications = BookingModificationRequest.objects.filter(
        status__in=pending_modification_statuses
    ).count()
    new_contacts = ContactMessage.objects.filter(status=ContactMessage.Status.NEW).count()
    unread_notifications = Notification.objects.filter(
        status=Notification.Status.ACTIVE, is_read=False
    ).count()
    failed_webhooks = HostawayWebhookEvent.objects.filter(
        status=HostawayWebhookEvent.Status.FAILED
    ).count()
    failed_emails = EmailDelivery.objects.filter(status=EmailDelivery.Status.FAILED).count()
    properties_need_attention = Property.objects.filter(
        Q(is_visible=False) | Q(publish_blockers__isnull=False) & ~Q(publish_blockers=[])
    ).count()

    actions = [
        {
            "severity": "critical",
            "count": reservations_attention,
            "title": "حجوزات تحتاج تدخّلًا",
            "description": "حالة إنشاء أو مزامنة غير مؤكدة وتحتاج قرارًا يدويًا.",
            "url": _url("admin:reservations_reservation_changelist"),
        },
        {
            "severity": "critical",
            "count": payment_attention,
            "title": "مدفوعات تحتاج مراجعة",
            "description": "عملية فاشلة أو قيد المراجعة خلال آخر 30 يومًا.",
            "url": _url("admin:payments_paymentattempt_changelist"),
        },
        {
            "severity": "critical",
            "count": failed_webhooks,
            "title": "Webhooks فاشلة",
            "description": "راجع الاستقبال قبل أن يؤثر على تحديث الحجوزات.",
            "url": _url("admin:integrations_hostawaywebhookevent_changelist"),
        },
        {
            "severity": "warning",
            "count": pending_modifications,
            "title": "طلبات تعديل معلّقة",
            "description": "تمديد أو تعديل يحتاج موافقة أو استكمال التنفيذ.",
            "url": _url("admin:reservations_bookingmodificationrequest_changelist"),
        },
        {
            "severity": "warning",
            "count": stale_pending_payments,
            "title": "مدفوعات معلّقة منذ أكثر من 30 دقيقة",
            "description": "تحقق من حالتها لدى المزود قبل التواصل مع العميل.",
            "url": _url("admin:payments_paymentattempt_changelist"),
        },
        {
            "severity": "warning",
            "count": failed_emails,
            "title": "رسائل بريد لم تُسلّم",
            "description": "إشعارات عميل فشلت وتحتاج إعادة متابعة.",
            "url": _url("admin:notifications_emaildelivery_changelist"),
        },
        {
            "severity": "warning",
            "count": properties_need_attention,
            "title": "وحدات غير جاهزة للنشر",
            "description": "بيانات أو محتوى أو إعداد ظهور يحتاج إكمالًا.",
            "url": _url("admin:properties_property_changelist"),
        },
        {
            "severity": "info",
            "count": new_contacts,
            "title": "رسائل عملاء تنتظر الرد",
            "description": "حوّل الرسالة إلى قيد المتابعة فور بدء معالجتها.",
            "url": _url("admin:core_contactmessage_changelist"),
        },
        {
            "severity": "info",
            "count": unread_notifications,
            "title": "تنبيهات غير مقروءة",
            "description": "راجع مركز التنبيهات وأغلق ما تم التعامل معه.",
            "url": _url("notifications:center"),
        },
    ]
    action_queue = [action for action in actions if action["count"]]

    funnel_counts = [
        {
            "key": "intent",
            "label": "طلبات بدأت",
            "total": BookingIntent.objects.filter(created_at__gte=rolling_start).count(),
        },
        {
            "key": "payment",
            "label": "وصلت للدفع",
            "total": BookingIntent.objects.filter(
                created_at__gte=rolling_start,
                status__in=(
                    BookingIntent.Status.AWAITING_PAYMENT,
                    BookingIntent.Status.PAYMENT_VERIFIED,
                    BookingIntent.Status.COMPLETED,
                ),
            ).count(),
        },
        {
            "key": "paid",
            "label": "دفع ناجح",
            "total": PaymentAttempt.objects.filter(
                created_at__gte=rolling_start,
                status=PaymentAttempt.Status.SUCCEEDED,
            ).count(),
        },
        {
            "key": "confirmed",
            "label": "حجز مؤكد",
            "total": Reservation.objects.filter(
                created_at__gte=rolling_start,
                normalized_status__in=active_reservation_statuses,
            ).count(),
        },
    ]
    funnel_maximum = max((step["total"] for step in funnel_counts), default=0) or 1
    for step in funnel_counts:
        step["width"] = max(6, round((step["total"] / funnel_maximum) * 100))
    confirmed_rolling = funnel_counts[-1]["total"]
    conversion_rate = (
        round((confirmed_rolling / funnel_counts[0]["total"]) * 100)
        if funnel_counts[0]["total"]
        else 0
    )

    legal_pages = {
        page.slug: page
        for page in SitePage.objects.filter(
            slug__in=("terms", "privacy", "cancellation", "cookies")
        )
    }
    return {
        "today": today,
        "properties_total": Property.objects.count(),
        "properties_visible": Property.objects.filter(is_visible=True).count(),
        "properties_need_attention": properties_need_attention,
        "confirmed_month": Reservation.objects.filter(
            normalized_status=Reservation.Status.CONFIRMED,
            created_at__date__gte=month_start,
        ).count(),
        "arrivals_today": Reservation.objects.filter(
            check_in=today,
            normalized_status__in=(Reservation.Status.CONFIRMED, Reservation.Status.MODIFIED),
        ).count(),
        "departures_today": Reservation.objects.filter(
            check_out=today,
            normalized_status__in=(Reservation.Status.CONFIRMED, Reservation.Status.MODIFIED),
        ).count(),
        "active_stays": Reservation.objects.filter(
            check_in__lte=today,
            check_out__gt=today,
            normalized_status__in=(Reservation.Status.CONFIRMED, Reservation.Status.MODIFIED),
        ).count(),
        "pending_payments": pending_payments,
        "stale_pending_payments": stale_pending_payments,
        "payment_attention": payment_attention,
        "reservations_attention": reservations_attention,
        "revenue": revenue,
        "revenue_by_currency": revenue_by_currency,
        "pending_modifications": pending_modifications,
        "new_contacts": new_contacts,
        "unread_notifications": unread_notifications,
        "failed_webhooks": failed_webhooks,
        "failed_emails": failed_emails,
        "action_queue": action_queue,
        "action_groups_count": len(action_queue),
        "action_items_total": sum(action["count"] for action in action_queue),
        "schedule": schedule_rows[:8],
        "schedule_total": len(schedule_rows),
        "week_end": week_end,
        "funnel": funnel_counts,
        "conversion_rate": conversion_rate,
        "trend": _trend_rows(),
        "recent": recent_rows,
        "latest_sync": latest_sync,
        "hostaway_configured": bool(
            settings.HOSTAWAY_ACCESS_TOKEN
            or (settings.HOSTAWAY_ACCOUNT_ID and settings.HOSTAWAY_API_SECRET)
        ),
        "hostaway_writes_enabled": any(
            (
                settings.HOSTAWAY_LIVE_BOOKING_ENABLED,
                settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED,
                settings.HOSTAWAY_LIVE_EXTENSION_ENABLED,
                settings.HOSTAWAY_LIVE_CANCELLATION_ENABLED,
            )
        ),
        "google": {
            "enabled": settings.GOOGLE_INTEGRATIONS_ENABLED,
            "ga4": bool(settings.GOOGLE_ANALYTICS_MEASUREMENT_ID),
            "gtm": bool(settings.GOOGLE_TAG_MANAGER_CONTAINER_ID),
            "search_console": bool(settings.GOOGLE_SITE_VERIFICATION),
        },
        "legal_pages": legal_pages,
        "urls": {
            "properties": _url("admin:properties_property_changelist"),
            "property_add": _url("admin:properties_property_add"),
            "customers": _url("admin_customers"),
            "bookings": _url("admin:reservations_reservation_changelist"),
            "booking_intents": _url("admin:reservations_bookingintent_changelist"),
            "modifications": _url("admin:reservations_bookingmodificationrequest_changelist"),
            "payments": _url("admin:payments_paymentattempt_changelist"),
            "contacts": _url("admin:core_contactmessage_changelist"),
            "content": _url("admin:core_sitepage_changelist"),
            "faq": _url("admin:core_faqitem_changelist"),
            "reviews": _url("admin:reviews_review_changelist"),
            "settings": _url("admin:core_sitesetting_changelist"),
            "operations": _url("notifications:dashboard"),
            "notifications": _url("notifications:center"),
            "system": _url("notifications:system_status"),
            "hostaway": _url("admin:integrations_integration_health"),
            "marketing": _url("marketing:diagnostics"),
            "seo": _url("marketing:seo_dashboard"),
            "site": _url("core:home"),
        },
    }
