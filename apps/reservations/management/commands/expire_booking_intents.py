"""Expire local pre-booking records without contacting Hostaway or payment."""

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.reservations.models import BookingIntent, BookingQuote


class Command(BaseCommand):
    help = "Expire stale BookingQuote and incomplete BookingIntent records."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        now = timezone.now()
        quote_query = BookingQuote.objects.filter(
            status=BookingQuote.Status.ACTIVE,
            expires_at__lte=now,
        )
        intent_query = BookingIntent.objects.filter(expires_at__lte=now).exclude(
            status__in=(
                BookingIntent.Status.COMPLETED,
                BookingIntent.Status.CANCELLED,
                BookingIntent.Status.EXPIRED,
            )
        )
        quote_count = quote_query.count()
        intent_count = intent_query.count()
        if not options["dry_run"]:
            with transaction.atomic():
                quote_query.update(status=BookingQuote.Status.EXPIRED, updated_at=now)
                intent_query.update(status=BookingIntent.Status.EXPIRED, updated_at=now)
        prefix = "would expire" if options["dry_run"] else "expired"
        self.stdout.write(f"Booking quotes {prefix}: {quote_count}")
        self.stdout.write(f"Booking intents {prefix}: {intent_count}")
        self.stdout.write("Hostaway changes: 0")
        self.stdout.write("Payment changes: 0")
