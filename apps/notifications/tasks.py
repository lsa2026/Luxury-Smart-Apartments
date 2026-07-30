"""Celery tasks for email and operational notifications."""

from typing import Any

from celery import shared_task
from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from apps.notifications.models import EmailDelivery, Notification
from apps.notifications.services.email import queue_email, send_queued_email


@shared_task(
    bind=True,
    name="apps.notifications.tasks.send_email_delivery_task",
    max_retries=3,
    soft_time_limit=45,
    time_limit=60,
)
def send_email_delivery_task(self: Any, delivery_id: str) -> dict[str, str | bool]:
    if not settings.EMAIL_DELIVERY_ENABLED:
        return {"sent": False, "code": "email_delivery_disabled"}
    result = send_queued_email(delivery_id)
    delivery = EmailDelivery.objects.only("last_error_code").get(pk=delivery_id)
    if not result.sent and delivery.last_error_code.startswith("transient_"):
        raise self.retry(
            countdown=settings.EMAIL_RETRY_DELAY_SECONDS,
            exc=RuntimeError(result.code),
        )
    return {"sent": result.sent, "code": result.code}


@shared_task(
    name="apps.notifications.tasks.process_email_queue_task",
    soft_time_limit=50,
    time_limit=60,
)
def process_email_queue_task(limit: int = 100) -> dict[str, int | str]:
    if not settings.EMAIL_DELIVERY_ENABLED:
        return {"status": "disabled", "processed": 0}
    ids = list(
        EmailDelivery.objects.filter(status=EmailDelivery.Status.QUEUED)
        .order_by("queued_at")
        .values_list("pk", flat=True)[: max(1, min(limit, 500))]
    )
    processed = sum(int(send_queued_email(delivery_id).sent) for delivery_id in ids)
    return {"status": "completed", "processed": processed}


@shared_task(
    name="apps.notifications.tasks.cleanup_expired_notifications_task",
    soft_time_limit=50,
    time_limit=60,
)
def cleanup_expired_notifications_task(limit: int = 1000) -> dict[str, int | str]:
    ids = list(
        Notification.objects.filter(
            expires_at__lte=timezone.now(),
            status=Notification.Status.ACTIVE,
        ).values_list("pk", flat=True)[: max(1, min(limit, 5000))]
    )
    updated = Notification.objects.filter(pk__in=ids).update(status=Notification.Status.ARCHIVED)
    return {"status": "completed", "archived": updated}


@shared_task(
    name="apps.notifications.tasks.send_daily_operations_summary_task",
    soft_time_limit=50,
    time_limit=60,
)
def send_daily_operations_summary_task() -> dict[str, int | str]:
    if not settings.ADMIN_NOTIFICATION_EMAIL_ENABLED or not settings.OPERATIONS_EMAIL:
        return {"status": "disabled", "queued": 0}
    today = timezone.localdate()
    counts = dict(
        Notification.objects.filter(created_at__date=today)
        .values_list("severity")
        .annotate(total=Count("id"))
    )
    delivery = queue_email(
        message_type="daily_operations_summary",
        recipient=settings.OPERATIONS_EMAIL,
        recipient_source="operations",
        recipient_reference="operations",
        language="ar",
        idempotency_key=f"daily-operations:{today.isoformat()}:{sum(counts.values())}",
    )
    return {"status": delivery.status, "queued": int(delivery.status == "queued")}
