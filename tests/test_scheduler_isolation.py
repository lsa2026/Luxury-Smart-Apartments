"""Beat must dispatch without importing delivery or payment applications."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

from scheduler_runtime.schedule import build_beat_schedule

ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_ENV = {
    "REDIS_URL": "redis://127.0.0.1:6379/0",
    "HOSTAWAY_PRICE_CALENDAR_SYNC_ENABLED": "true",
    "HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED": "true",
    "TRUSTINDEX_REVIEW_SYNC_ENABLED": "true",
    "CELERY_SYNC_DISPATCH_ENABLED": "true",
    "EMAIL_TASK_SCHEDULE_ENABLED": "true",
}


def run_isolated(code, overrides=None):
    # No inherited credentials and no network calls in these child processes.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    }
    environment.update(SCHEDULE_ENV)
    environment.update(overrides or {})
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_scheduler_ignores_inherited_provider_flags_and_missing_secrets():
    result = run_isolated(
        """
import sys
from unittest.mock import Mock
from celery.beat import Scheduler, ScheduleEntry
from scheduler_runtime.app import app
app.loader.import_default_modules()
assert 'config.settings.production' not in sys.modules
assert 'apps.payments.tasks' not in sys.modules
assert 'apps.notifications.tasks' not in sys.modules
assert app.fixups == []
assert app.conf.timezone == 'Asia/Riyadh'
assert app.conf.beat_schedule['indicative-rates']['schedule'].hour == {16}
assert app.conf.beat_schedule['indicative-rates']['schedule'].minute == {30}
assert 'process-email-queue' in app.conf.beat_schedule
assert 'daily-operations-summary' in app.conf.beat_schedule
assert 'hostaway-properties' not in app.conf.beat_schedule
assert 'hostaway-paid-booking-reconciliation' not in app.conf.beat_schedule
app.send_task = Mock()
scheduler = Scheduler(app=app, lazy=True)
entry = ScheduleEntry(name='indicative-rates', app=app,
    **app.conf.beat_schedule['indicative-rates'])
scheduler.apply_async(entry, advance=False)
assert app.send_task.call_args.args[0] == entry.task
print('isolated dispatch verified')
""",
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "EMAIL_DELIVERY_ENABLED": "true",
            "EMAIL_USE_TLS": "true",
            "EMAIL_USE_SSL": "true",
            "HYPERPAY_ENABLED": "true",
            "ULTRAMSG_ENABLED": "true",
            "GOOGLE_SIGN_IN_ENABLED": "true",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "isolated dispatch verified" in result.stdout


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"REDIS_URL": ""}, "REDIS_URL must be a valid Redis broker URL"),
        ({"REDIS_URL": "https://example.invalid"}, "REDIS_URL must be a valid Redis"),
        ({"HOSTAWAY_PRICE_CALENDAR_SYNC_HOUR": "24"}, "Invalid daily calendar sync time"),
        ({"HOSTAWAY_PRICE_CALENDAR_SYNC_MINUTE": "60"}, "Invalid daily calendar sync time"),
        ({"EMAIL_TASK_SCHEDULE_ENABLED": "maybe"}, "must be an explicit boolean"),
        ({"BOOKING_EXPIRATION_INTERVAL_MINUTES": "0"}, "must be a positive integer"),
    ],
)
def test_scheduler_still_validates_its_own_requirements(overrides, error):
    result = run_isolated("from scheduler_runtime.app import app", overrides)
    assert result.returncode != 0
    assert error in result.stderr


def test_production_still_requires_email_password():
    result = run_isolated(
        "import config.settings.production",
        {
            "DJANGO_SECRET_KEY": "test-only-not-used-outside-this-child-process",
            "DATABASE_URL": "sqlite:///:memory:",
            "DJANGO_ALLOWED_HOSTS": "example.invalid",
            "EMAIL_DELIVERY_ENABLED": "true",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "DEFAULT_FROM_EMAIL": "test@example.invalid",
            "EMAIL_HOST": "smtp.example.invalid",
            "EMAIL_HOST_USER": "test@example.invalid",
        },
    )
    assert result.returncode != 0
    assert "required settings are missing: EMAIL_HOST_PASSWORD" in result.stderr


def test_django_and_standalone_use_one_schedule_definition():
    values = {
        key: getattr(settings, key)
        for key in (
            "HOSTAWAY_AUTO_SYNC_ENABLED",
            "HOSTAWAY_AUTO_SYNC_INTERVAL_MINUTES",
            "HOSTAWAY_PRICE_CALENDAR_SYNC_ENABLED",
            "HOSTAWAY_PRICE_CALENDAR_SYNC_HOUR",
            "HOSTAWAY_PRICE_CALENDAR_SYNC_MINUTE",
            "HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED",
            "HOSTAWAY_WEBHOOK_PROCESSING_ENABLED",
            "HOSTAWAY_WEBHOOK_PROCESS_INTERVAL_MINUTES",
            "BOOKING_EXPIRATION_INTERVAL_MINUTES",
            "TRUSTINDEX_REVIEW_SYNC_ENABLED",
            "CELERY_SYNC_DISPATCH_ENABLED",
            "EMAIL_TASK_SCHEDULE_ENABLED",
        )
    }
    assert settings.CELERY_BEAT_SCHEDULE == build_beat_schedule(values)
    values.update({key: True for key in values if key.endswith("_ENABLED")})
    schedule = build_beat_schedule(values)
    assert len(schedule) == 10
    assert schedule["daily-operations-summary"]["schedule"].hour == {8}
    assert schedule["trustindex-review-metrics"]["schedule"].hour == {17}
    assert schedule["website-reservation-statuses"]["schedule"] == 1800
    assert schedule["process-email-queue"]["schedule"] == 60


def test_blueprint_keeps_scheduler_environment_separate():
    blueprint = (ROOT / "render.yaml").read_text()
    scheduler = blueprint.split("    name: lsa-beat\n", 1)[1]
    assert "celery -A scheduler_runtime.app:app beat" in scheduler
    assert "envVars: *" not in scheduler
    for unnecessary in (
        "EMAIL_HOST_PASSWORD",
        "EMAIL_DELIVERY_ENABLED",
        "HYPERPAY",
        "DATABASE_URL",
        "HOSTAWAY_API_SECRET",
        "HOSTAWAY_ACCOUNT_ID",
        "DJANGO_SETTINGS_MODULE",
    ):
        assert unnecessary not in scheduler
    assert "EMAIL_TASK_SCHEDULE_ENABLED" in scheduler
    assert 'value: "16"' in scheduler
    assert 'value: "30"' in scheduler
