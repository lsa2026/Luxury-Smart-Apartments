"""Supervise UAT web + isolated poller without another paid Render service."""

import os
import signal
import subprocess
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.payments.hyperbill import HyperBillError, require_sandbox


class Command(BaseCommand):
    help = "Run Gunicorn and the HyperBill-only UAT poller together. Never production."

    def handle(self, *args, **options):
        try:
            require_sandbox()
        except HyperBillError as exc:
            raise CommandError(str(exc)) from None
        if sys.platform != "linux" or not settings.CACHE_URL:
            raise CommandError("hyperbill_uat_linux_and_shared_cache_required")
        if settings.HYPERBILL_RECONCILIATION_ENABLED:
            raise CommandError("hyperbill_uat_poller_requires_celery_dispatch_disabled")
        port = os.environ.get("PORT", "")
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise CommandError("hyperbill_uat_port_required")
        commands = [
            [
                sys.executable, "-m", "gunicorn", "config.wsgi:application",
                "--bind", f"0.0.0.0:{port}", "--workers", "2", "--threads", "2",
                "--timeout", "60", "--graceful-timeout", "30",
                "--access-logfile", "-", "--error-logfile", "-",
                # Callback authentication is in the URL: never log request paths.
                "--access-logformat", "%(h)s %(s)s %(b)s %(L)s",
            ],
            [sys.executable, "manage.py", "run_hyperbill_worker"],
        ]
        children = []
        stopping = False

        def stop_requested(signum, frame):
            nonlocal stopping
            stopping = True

        previous = {s: signal.signal(s, stop_requested) for s in (signal.SIGTERM, signal.SIGINT)}
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            for command in commands:
                children.append(subprocess.Popen(command, env=env))
            self.stdout.write("HYPERBILL_UAT_WEB_AND_POLLER_STARTED")
            while not stopping:
                if any(child.poll() is not None for child in children):
                    raise CommandError("hyperbill_uat_child_stopped_restart_required")
                time.sleep(1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
            for child in children:
                try:
                    child.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
