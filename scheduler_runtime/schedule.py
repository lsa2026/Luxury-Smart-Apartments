"""One schedule definition shared by Django and the dispatch-only scheduler."""

from collections.abc import Mapping
from typing import Any

from celery.schedules import crontab


def build_beat_schedule(settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    schedule = {}
    if settings["HOSTAWAY_AUTO_SYNC_ENABLED"]:
        schedule["hostaway-properties"] = {
            "task": "apps.integrations.tasks.sync_hostaway_properties_task",
            "schedule": settings["HOSTAWAY_AUTO_SYNC_INTERVAL_MINUTES"] * 60,
        }
    if settings["HOSTAWAY_PRICE_CALENDAR_SYNC_ENABLED"]:
        schedule["indicative-rates"] = {
            "task": "apps.integrations.tasks.refresh_indicative_rates_task",
            "schedule": crontab(
                hour=settings["HOSTAWAY_PRICE_CALENDAR_SYNC_HOUR"],
                minute=settings["HOSTAWAY_PRICE_CALENDAR_SYNC_MINUTE"],
            ),
        }
    if settings["HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED"]:
        schedule["website-reservation-statuses"] = {
            "task": "apps.integrations.tasks.refresh_website_reservations_task",
            "schedule": 30 * 60,
        }
    if settings["HOSTAWAY_WEBHOOK_PROCESSING_ENABLED"]:
        schedule["hostaway-webhooks"] = {
            "task": "apps.integrations.tasks.process_hostaway_webhooks_task",
            "schedule": settings["HOSTAWAY_WEBHOOK_PROCESS_INTERVAL_MINUTES"] * 60,
        }
    schedule["expire-booking-objects"] = {
        "task": "apps.integrations.tasks.expire_booking_objects_task",
        "schedule": settings["BOOKING_EXPIRATION_INTERVAL_MINUTES"] * 60,
    }
    if settings["TRUSTINDEX_REVIEW_SYNC_ENABLED"]:
        schedule["trustindex-review-metrics"] = {
            "task": "apps.reviews.tasks.sync_trustindex_review_metrics_task",
            "schedule": crontab(hour=17, minute=0),
        }
    if settings["CELERY_SYNC_DISPATCH_ENABLED"]:
        schedule["integration-scheduler-health"] = {
            "task": "apps.integrations.tasks.check_sync_health_task",
            "schedule": 5 * 60,
        }
    if settings["EMAIL_TASK_SCHEDULE_ENABLED"]:
        schedule.update(
            {
                "process-email-queue": {
                    "task": "apps.notifications.tasks.process_email_queue_task",
                    "schedule": 60,
                },
                "cleanup-expired-notifications": {
                    "task": "apps.notifications.tasks.cleanup_expired_notifications_task",
                    "schedule": 24 * 60 * 60,
                },
                "daily-operations-summary": {
                    "task": "apps.notifications.tasks.send_daily_operations_summary_task",
                    "schedule": crontab(hour=8, minute=0),
                },
            }
        )
    return schedule
