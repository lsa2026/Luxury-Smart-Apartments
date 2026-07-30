"""Verify one listing's calendar and price without creating or storing a reservation."""

from datetime import date

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.integrations.hostaway.availability_validators import CalendarDay
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import Property
from apps.reservations.services.availability import (
    AvailabilityRequest,
    AvailabilityResult,
    AvailabilityService,
)


class Command(BaseCommand):
    help = "Read one Hostaway calendar and calculate one priceDetails v2 quote."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing-id", type=int, required=True)
        parser.add_argument("--check-in", type=date.fromisoformat)
        parser.add_argument("--check-out", type=date.fromisoformat)
        parser.add_argument("--guests", type=int, default=2)
        parser.add_argument("--scan-days", type=int, default=60)
        parser.add_argument("--stay-nights", type=int, default=2)
        parser.add_argument("--bypass-cache", action="store_true")
        parser.add_argument("--show-schema", action="store_true")
        parser.add_argument("--strict", action="store_true")
        parser.add_argument("--timeout", type=float)

    def handle(self, *args: object, **options: object) -> None:
        listing_id = options["listing_id"]
        property_obj = Property.objects.filter(hostaway_listing_id=listing_id).first()
        if property_obj is None:
            raise CommandError("The requested listing is not present in the local database.")
        if options["guests"] < 1:
            raise CommandError("--guests must be positive.")
        guests = min(options["guests"], property_obj.person_capacity or options["guests"])
        check_in = options["check_in"]
        check_out = options["check_out"]
        if (check_in is None) != (check_out is None):
            raise CommandError("--check-in and --check-out must be supplied together.")
        if options["timeout"] is not None and options["timeout"] <= 0:
            raise CommandError("--timeout must be positive.")

        before_counts = self._database_counts()
        try:
            with HostawayClient(timeout=options["timeout"]) as client:
                with AvailabilityService(client=client) as service:
                    if check_in is not None and check_out is not None:
                        request = AvailabilityRequest(
                            property=property_obj,
                            check_in=check_in,
                            check_out=check_out,
                            guests=guests,
                        )
                        result = service.check(
                            request,
                            bypass_cache=options["bypass_cache"],
                        )
                    else:
                        request, result, _calendar = service.find_first_available(
                            property_obj=property_obj,
                            start_date=timezone.localdate(),
                            scan_days=options["scan_days"],
                            stay_nights=options["stay_nights"],
                            guests=guests,
                            bypass_cache=options["bypass_cache"],
                        )
        except (HostawayError, ValueError) as exc:
            raise CommandError(f"Hostaway availability verification failed: {exc}") from exc

        after_counts = self._database_counts()
        if before_counts != after_counts:
            raise CommandError("Verification unexpectedly changed persisted business records.")
        self._write_report(
            property_obj=property_obj,
            request=request,
            result=result,
            show_schema=options["show_schema"],
        )
        if options["strict"] and result.reason_code == "calendar_incomplete":
            raise CommandError("Strict verification failed: calendar coverage is incomplete.")

    def _write_report(
        self,
        *,
        property_obj: Property,
        request: AvailabilityRequest | None,
        result: AvailabilityResult,
        show_schema: bool,
    ) -> None:
        self.stdout.write("Hostaway availability verification (read-only)")
        self.stdout.write(f"Listing ID: {property_obj.hostaway_listing_id}")
        if request:
            self.stdout.write(
                f"Test period: {request.check_in.isoformat()} -> {request.check_out.isoformat()}"
            )
        else:
            calendar_days = result.calendar_document.days if result.calendar_document else ()
            if calendar_days:
                self.stdout.write(
                    "Scan period: "
                    f"{min(day.date for day in calendar_days).isoformat()} -> "
                    f"{max(day.date for day in calendar_days).isoformat()}"
                )
        self.stdout.write(f"Nights: {result.nights}")
        self.stdout.write(f"Availability: {result.reason_code}")
        self.stdout.write(
            f"Calendar response: {result.calendar_duration_ms} ms "
            f"cache={'hit' if result.calendar_cache_hit else 'miss'}"
        )
        if result.calendar_document:
            self._write_calendar_constraints(result.calendar_document.days)
        if result.quote:
            self.stdout.write(f"Currency: {result.quote.currency}")
            self.stdout.write(f"Total price: {result.quote.total_price}")
            self.stdout.write(f"Price components: {len(result.quote.components)}")
            component_types = sorted({component.type for component in result.quote.components})
            self.stdout.write(f"Component types: {', '.join(component_types) or 'none'}")
            self.stdout.write(
                f"Price response: {result.price_duration_ms} ms "
                f"cache={'hit' if result.price_cache_hit else 'miss'}"
            )
        else:
            self.stdout.write("Price request: not sent")
        self.stdout.write("Reservation created: no")
        self.stdout.write("Hostaway calendar modified: no")

        if show_schema and result.calendar_document:
            self.stdout.write("Calendar envelope fields:")
            for field in sorted(result.calendar_document.envelope_fields):
                self.stdout.write(f"  - {field}")
            self.stdout.write("Calendar day field types:")
            for field, type_name in result.calendar_document.day_field_types:
                self.stdout.write(f"  - {field}: {type_name}")
            if result.quote:
                self.stdout.write("Price result field types:")
                for field, type_name in result.quote.result_field_types:
                    self.stdout.write(f"  - {field}: {type_name}")
                self.stdout.write("Price component field types:")
                for field, type_name in result.quote.component_field_types:
                    self.stdout.write(f"  - {field}: {type_name}")

    def _write_calendar_constraints(self, days: tuple[CalendarDay, ...]) -> None:
        received = len(days)
        available = sum(getattr(day, "is_available", None) is True for day in days)
        closed_arrival = sum(getattr(day, "closed_on_arrival", None) is True for day in days)
        closed_departure = sum(getattr(day, "closed_on_departure", None) is True for day in days)
        minimum_stays = [
            value for day in days if (value := getattr(day, "minimum_stay", None)) is not None
        ]
        maximum_stays = [
            value for day in days if (value := getattr(day, "maximum_stay", None)) is not None
        ]
        self.stdout.write(f"Calendar days received: {received}")
        self.stdout.write(f"Days marked available: {available}")
        self.stdout.write(f"Closed-on-arrival days: {closed_arrival}")
        self.stdout.write(f"Closed-on-departure days: {closed_departure}")
        if minimum_stays:
            self.stdout.write(f"Minimum-stay range: {min(minimum_stays)}..{max(minimum_stays)}")
        if maximum_stays:
            self.stdout.write(f"Maximum-stay range: {min(maximum_stays)}..{max(maximum_stays)}")

    @staticmethod
    def _database_counts() -> tuple[int, int, int]:
        from apps.properties.models import PropertyImage
        from apps.reviews.models import Review

        return (
            Property.objects.count(),
            PropertyImage.objects.count(),
            Review.objects.count(),
        )
