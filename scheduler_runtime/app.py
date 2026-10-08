"""Dispatch-only Celery beat: no Django, SMTP, payment or Hostaway credentials.

The worker resolves task names and executes them with its own validated settings.
Do not enable Django fixups or autodiscover application tasks in this process.
"""

import os
from urllib.parse import urlparse

from celery import Celery

from scheduler_runtime.schedule import build_beat_schedule


def flag(name: str) -> bool:
    raw = os.environ.get(name, "false").strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be an explicit boolean value.")


def positive_minutes(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    value = int(raw) if raw else default
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


broker_url = os.environ.get("REDIS_URL", "").strip()
parsed_broker = urlparse(broker_url)
if parsed_broker.scheme not in {"redis", "rediss"} or not parsed_broker.hostname:
    raise ValueError("REDIS_URL must be a valid Redis broker URL for the scheduler.")

schedule_settings = {
    key: flag(key)
    for key in (
        "HOSTAWAY_AUTO_SYNC_ENABLED",
        "HOSTAWAY_PRICE_CALENDAR_SYNC_ENABLED",
        "HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED",
        "HOSTAWAY_WEBHOOK_PROCESSING_ENABLED",
        "TRUSTINDEX_REVIEW_SYNC_ENABLED",
        "CELERY_SYNC_DISPATCH_ENABLED",
        "EMAIL_TASK_SCHEDULE_ENABLED",
    )
}
schedule_settings.update(
    {
        key: positive_minutes(key, default)
        for key, default in (
            ("HOSTAWAY_AUTO_SYNC_INTERVAL_MINUTES", 5),
            ("HOSTAWAY_WEBHOOK_PROCESS_INTERVAL_MINUTES", 1),
            ("BOOKING_EXPIRATION_INTERVAL_MINUTES", 5),
        )
    }
)
schedule_settings["HOSTAWAY_PRICE_CALENDAR_SYNC_HOUR"] = int(
    os.environ.get("HOSTAWAY_PRICE_CALENDAR_SYNC_HOUR", "16")
)
schedule_settings["HOSTAWAY_PRICE_CALENDAR_SYNC_MINUTE"] = int(
    os.environ.get("HOSTAWAY_PRICE_CALENDAR_SYNC_MINUTE", "30")
)
if not 0 <= schedule_settings["HOSTAWAY_PRICE_CALENDAR_SYNC_HOUR"] <= 23 or not (
    0 <= schedule_settings["HOSTAWAY_PRICE_CALENDAR_SYNC_MINUTE"] <= 59
):
    raise ValueError("Invalid daily calendar sync time.")

# An inherited DJANGO_SETTINGS_MODULE must not pull production startup checks
# into beat. This does not alter or bypass validation in web/worker processes.
app = Celery("luxury_smart_apartments_scheduler", broker=broker_url, fixups=[])
app.conf.update(
    timezone="Asia/Riyadh",
    enable_utc=True,
    beat_schedule=build_beat_schedule(schedule_settings),
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
)
