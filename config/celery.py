"""Celery application configured from Django settings."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

app = Celery("luxury_smart_apartments")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
