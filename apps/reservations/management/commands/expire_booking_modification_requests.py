"""Expire local modification requests without touching reservations or Hostaway."""

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.reservations.services.expiration import expire_booking_objects


class Command(BaseCommand):
    help = "Expire unfinished booking modification requests."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args: object, **options: object) -> None:
        limit = options["limit"]
        if not 1 <= limit <= 5000:
            raise CommandError("--limit must be between 1 and 5000.")
        result = expire_booking_objects(
            dry_run=bool(options["dry_run"]),
            limit=limit,
            include_prebooking=False,
        )
        self.stdout.write(f"Eligible modification requests: {result.modifications}")
        if options["dry_run"]:
            self.stdout.write("Updated: 0")
            self.stdout.write("Hostaway calls: 0")
            return
        self.stdout.write(f"Updated: {result.modifications}")
        self.stdout.write("Hostaway calls: 0")
