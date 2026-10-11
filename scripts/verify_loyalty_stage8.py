"""Fail-closed test runner: local SQLite, offline, no inherited provider credentials."""

import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.update(
    {
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DATABASE_URL": "sqlite:///:memory:",
        "DJANGO_SECRET_KEY": "local-loyalty-stage-eight-test-only",
        "REDIS_URL": "",
        "CACHE_URL": "",
        "CELERY_BROKER_URL": "memory://",
        "CELERY_TASK_ALWAYS_EAGER": "false",
        "LOYALTY_ENABLED": "false",
        "LOYALTY_LAUNCH_AT": "",
        "LOYALTY_BACKUP_ENABLED": "false",
        "LOYALTY_BACKUP_LOCAL_REHEARSAL": "false",
        "LOYALTY_BACKUP_LOCAL_PATH": "",
        "LOYALTY_BACKUP_SOURCE_ID": "",
        "LOYALTY_BACKUP_DATABASE_URL": "",
        "LOYALTY_BACKUP_PROJECT_REF": "",
        "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    }
)


def reject_network(*args, **kwargs):
    raise AssertionError("External network connections are forbidden by the stage-eight runner.")


socket.socket.connect = reject_network
socket.socket.connect_ex = reject_network
socket.create_connection = reject_network

import pytest  # noqa: E402

if sys.argv[1:] == ["--check-schema"]:
    import django
    from django.core.management import call_command

    django.setup()
    call_command("makemigrations", check=True, dry_run=True)
    call_command("check")
    raise SystemExit(0)

raise SystemExit(pytest.main(sys.argv[1:] or ["tests/test_loyalty_stage8.py", "-q"]))
