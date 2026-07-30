"""Expire local pre-booking records without contacting Hostaway or payment."""

from django.core.management.base import BaseCommand, CommandParser

from apps.reservations.services.expiration import expire_booking_objects


class Command(BaseCommand):
    help = "Expire stale BookingQuote and incomplete BookingIntent records."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        result = expire_booking_objects(
            dry_run=bool(options["dry_run"]),
            include_modifications=False,
        )
        prefix = "would expire" if options["dry_run"] else "expired"
        self.stdout.write(f"Booking quotes {prefix}: {result.quotes}")
        self.stdout.write(f"Booking intents {prefix}: {result.intents}")
        self.stdout.write("Hostaway changes: 0")
        self.stdout.write("Payment changes: 0")
