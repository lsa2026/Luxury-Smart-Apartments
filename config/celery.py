"""Celery application configured from Django settings."""

import os

from celery import Celery
from celery.signals import worker_ready

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

app = Celery("luxury_smart_apartments")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@worker_ready.connect
def queue_owner_backup_replacement(sender=None, **kwargs):
    """Dispatch only an explicit owner request; empty by default, never a schedule."""
    request_id = os.environ.get("LSA_LOYALTY_REPLACEMENT_REQUEST", "")
    if request_id and sender is not None:
        sender.app.send_task(
            "apps.loyalty.tasks.owner_replace_backup_task",
            kwargs={"request_id": request_id},
            task_id=f"{request_id}-backup",
        )
