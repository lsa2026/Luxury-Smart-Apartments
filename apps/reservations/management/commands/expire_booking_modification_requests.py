"""Expire local modification requests without touching reservations or Hostaway."""

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.reservations.models import BookingModificationRequest


class Command(BaseCommand):
    help = "Expire unfinished booking modification requests."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args: object, **options: object) -> None:
        limit = options["limit"]
        if not 1 <= limit <= 5000:
            raise CommandError("--limit must be between 1 and 5000.")
        terminal = (
            BookingModificationRequest.Status.COMPLETED,
            BookingModificationRequest.Status.REJECTED,
            BookingModificationRequest.Status.EXPIRED,
        )
        ids = list(
            BookingModificationRequest.objects.filter(expires_at__lte=timezone.now())
            .exclude(status__in=terminal)
            .order_by("expires_at")
            .values_list("pk", flat=True)[:limit]
        )
        self.stdout.write(f"Eligible modification requests: {len(ids)}")
        if options["dry_run"]:
            self.stdout.write("Updated: 0")
            self.stdout.write("Hostaway calls: 0")
            return
        updated = BookingModificationRequest.objects.filter(pk__in=ids).update(
            status=BookingModificationRequest.Status.EXPIRED,
            updated_at=timezone.now(),
        )
        self.stdout.write(f"Updated: {updated}")
        self.stdout.write("Hostaway calls: 0")
