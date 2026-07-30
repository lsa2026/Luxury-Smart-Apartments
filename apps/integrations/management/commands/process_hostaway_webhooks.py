"""Process sanitized Hostaway webhook events outside the HTTP request."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.webhook_processor import (
    claim_webhook_event_ids,
    process_webhook_event,
)


class Command(BaseCommand):
    help = "Process queued Hostaway Unified Webhook events."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--event-id")
        parser.add_argument("--retry-failed", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        limit = options["limit"]
        if limit < 1 or limit > 500:
            raise CommandError("--limit must be between 1 and 500.")
        event_ids = claim_webhook_event_ids(
            limit=limit,
            event_id=options["event_id"],
            retry_failed=options["retry_failed"],
        )
        if options["dry_run"]:
            self.stdout.write(f"Eligible events: {len(event_ids)}")
            self.stdout.write("Database changes: 0")
            self.stdout.write("Hostaway calls: 0")
            return
        if not settings.HOSTAWAY_WEBHOOK_PROCESSING_ENABLED:
            raise CommandError("hostaway_webhook_processing_disabled")
        counts = {"processed": 0, "stale": 0, "ignored": 0, "retryable": 0, "failed": 0}
        with HostawayClient() as client:
            for event_id in event_ids:
                result = process_webhook_event(
                    event_id,
                    client=client,
                    retry_failed=options["retry_failed"],
                )
                if result.code in counts:
                    counts[result.code] += 1
        for key, value in counts.items():
            self.stdout.write(f"{key}: {value}")
