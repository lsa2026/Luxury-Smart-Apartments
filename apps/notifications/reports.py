"""PostgreSQL-only aggregate reports for operations."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.core.cache import cache
from django.db.models import Avg, Count
from django.utils import timezone

from apps.core.models import ContactMessage
from apps.integrations.models import HostawayWebhookEvent, IntegrationSyncRun
from apps.notifications.models import EmailDelivery, Notification
from apps.properties.models import Property, PropertyImage
from apps.reservations.models import (
    BookingIntent,
    BookingModificationRequest,
    BookingQuote,
    Reservation,
)
from apps.reviews.models import Review


def _average_rating_out_of_five() -> Decimal | None:
    """Return the Hostaway ten-point review average on the UI's five-point scale."""

    average = Review.objects.aggregate(value=Avg("rating"))["value"]
    if average is None:
        return None
    return (Decimal(average) / Decimal("2")).quantize(Decimal("0.1"))


def report_period(period: str, start: str = "", end: str = "") -> tuple[date, date]:
    today = timezone.localdate()
    if period == "today":
        return today, today
    if period == "7":
        return today - timedelta(days=6), today
    if period == "custom":
        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
        except ValueError:
            return today - timedelta(days=29), today
        if start_date <= end_date and (end_date - start_date).days <= 366:
            return start_date, end_date
    return today - timedelta(days=29), today


def operations_report(start_date: date, end_date: date) -> dict[str, Any]:
    cache_key = f"operations-report:v1:{start_date}:{end_date}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    date_range = (start_date, end_date)
    report = {
        "period_start": start_date,
        "period_end": end_date,
        "properties_total": Property.objects.count(),
        "properties_visible": Property.objects.filter(is_visible=True).count(),
        "properties_hidden": Property.objects.filter(is_visible=False).count(),
        "properties_archived": Property.objects.filter(
            hostaway_special_status__iexact="archived"
        ).count(),
        "images_total": PropertyImage.objects.count(),
        "reviews_total": Review.objects.count(),
        "average_rating": _average_rating_out_of_five(),
        "new_contacts": ContactMessage.objects.filter(
            status=ContactMessage.Status.NEW,
            created_at__date__range=date_range,
        ).count(),
        "quote_statuses": list(
            BookingQuote.objects.filter(created_at__date__range=date_range)
            .values("status")
            .annotate(total=Count("id"))
            .order_by("status")
        ),
        "intent_statuses": list(
            BookingIntent.objects.filter(created_at__date__range=date_range)
            .values("status")
            .annotate(total=Count("id"))
            .order_by("status")
        ),
        "reservation_statuses": list(
            Reservation.objects.filter(created_at__date__range=date_range)
            .values("normalized_status")
            .annotate(total=Count("id"))
            .order_by("normalized_status")
        ),
        "modification_statuses": list(
            BookingModificationRequest.objects.filter(created_at__date__range=date_range)
            .values("status")
            .annotate(total=Count("id"))
            .order_by("status")
        ),
        "failed_webhooks": HostawayWebhookEvent.objects.filter(
            status=HostawayWebhookEvent.Status.FAILED,
            created_at__date__range=date_range,
        ).count(),
        "email_statuses": list(
            EmailDelivery.objects.filter(created_at__date__range=date_range)
            .values("status")
            .annotate(total=Count("id"))
            .order_by("status")
        ),
        "notification_unread": Notification.objects.filter(
            status=Notification.Status.ACTIVE,
            is_read=False,
        ).count(),
        "top_reviewed": list(
            Property.objects.annotate(review_count=Count("reviews"))
            .filter(review_count__gt=0)
            .values("name_ar", "name_en", "review_count")[:5]
        ),
        "latest_sync": IntegrationSyncRun.objects.order_by("-started_at")
        .values("sync_type", "status", "started_at", "failed_count")
        .first(),
    }
    cache.set(cache_key, report, timeout=60)
    return report
