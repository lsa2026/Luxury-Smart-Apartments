"""Scheduled maintenance for Trustindex review display metrics."""

from celery import shared_task
from django.conf import settings


@shared_task(
    name="apps.reviews.tasks.sync_trustindex_review_metrics_task",
    soft_time_limit=90,
    time_limit=120,
)
def sync_trustindex_review_metrics_task() -> dict[str, str]:
    """Refresh cached public totals without fetching reviews during a page view."""
    if not settings.TRUSTINDEX_REVIEW_SYNC_ENABLED:
        return {"status": "disabled"}
    from apps.integrations.models import IntegrationSyncRun
    from apps.integrations.monitoring import run_display_command
    from apps.integrations.tasks import distributed_task_lock

    with distributed_task_lock("trustindex-review-metrics") as acquired:
        if not acquired:
            return {"status": "already_running"}
        return run_display_command(
            "sync_trustindex_review_metrics", IntegrationSyncRun.SyncType.TRUSTINDEX_METRICS
        )
