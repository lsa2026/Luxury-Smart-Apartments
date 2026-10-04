"""Isolated DB-signal polling: no generic Celery queue or Hostaway writes."""

import json
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from apps.payments.hyperbill import HyperBillError, require_sandbox
from apps.payments.hyperbill_tasks import reconcile_hyperbill_task
from apps.payments.models import HyperBillWebhookSignal


class Command(BaseCommand):
    help = "Poll durable HyperBill UAT signals and recover missed callbacks."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        try:
            require_sandbox()
        except HyperBillError as exc:
            raise CommandError(str(exc)) from None
        next_poll = 0.0
        self.stdout.write("HYPERBILL_UAT_WORKER_READY")
        while True:
            close_old_connections()
            now = time.monotonic()
            if (
                options["once"]
                or now >= next_poll
                or HyperBillWebhookSignal.objects.filter(processed_at__isnull=True).exists()
            ):
                result = reconcile_hyperbill_task.run()
                self.stdout.write("HYPERBILL_UAT_RECONCILIATION " + json.dumps(result))
                next_poll = time.monotonic() + 60
            if options["once"]:
                return
            # Bound callback retries, including provider outages, to at most once / 10s.
            time.sleep(10)
