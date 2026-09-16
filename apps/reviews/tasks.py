"""Scheduled maintenance for Trustindex review display metrics."""

from io import StringIO

from celery import shared_task
from django.core.management import call_command


@shared_task(
    name="apps.reviews.tasks.sync_trustindex_review_metrics_task",
    soft_time_limit=90,
    time_limit=120,
)
def sync_trustindex_review_metrics_task() -> dict[str, str]:
    """Refresh cached public totals without fetching reviews during a page view."""
    output = StringIO()
    call_command("sync_trustindex_review_metrics", stdout=output, stderr=output)
    return {"status": "completed", "summary": output.getvalue().strip()}
