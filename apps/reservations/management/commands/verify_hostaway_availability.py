"""Verify one listing's calendar and price without creating or storing a reservation."""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.integrations.hostaway.availability_validators import CalendarDay, CalendarDocument
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import Property
from apps.reservations.services.availability import (
    AvailabilityRequest,
    AvailabilityResult,
    AvailabilityService,
    classify_inventory,
    resolve_day_inventory,
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
        parser.add_argument("--diagnose-inventory", action="store_true")
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
        if not 1 <= options["scan_days"] <= 365:
            raise CommandError("--scan-days must be between 1 and 365.")

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
            diagnose_inventory=options["diagnose_inventory"],
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
        diagnose_inventory: bool,
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
            if diagnose_inventory:
                self._write_inventory_diagnosis(
                    result.calendar_document,
                    request=request,
                    stay_is_available=result.is_available,
                )
        if result.quote:
            self.stdout.write(f"Currency: {result.quote.currency}")
            self.stdout.write(f"Total price: {result.quote.total_price}")
            self.stdout.write(f"Price components: {len(result.quote.components)}")
            component_types = sorted({component.type for component in result.quote.components})
            self.stdout.write(f"Component types: {', '.join(component_types) or 'none'}")
            included_total = sum(
                (component.total if component.total is not None else component.value)
                for component in result.quote.components
                if component.is_included_in_total is True
            )
            excluded_count = sum(
                component.is_included_in_total is False for component in result.quote.components
            )
            self.stdout.write(f"Included components total: {included_total}")
            self.stdout.write(f"Not-included components: {excluded_count}")
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

    def _write_inventory_diagnosis(
        self,
        document: CalendarDocument,
        *,
        request: AvailabilityRequest | None,
        stay_is_available: bool,
    ) -> None:
        days = document.days
        is_available_distribution = Counter(
            "null" if day.is_available is None else "1" if day.is_available else "0" for day in days
        )
        status_distribution = Counter(day.status or "<empty>" for day in days)
        decisions = [resolve_day_inventory(day) for day in days]
        decision_strategies = {decision.strategy for decision in decisions}
        selected_strategy = (
            next(iter(decision_strategies)) if len(decision_strategies) == 1 else "mixed"
        )

        self.stdout.write("Inventory diagnosis (aggregate only):")
        self.stdout.write(f"  Inventory type: {classify_inventory(days)}")
        self.stdout.write(
            "  isAvailable distribution: "
            f"0={is_available_distribution['0']} "
            f"1={is_available_distribution['1']} "
            f"null={is_available_distribution['null']}"
        )
        self.stdout.write(
            "  status distribution: "
            + ", ".join(
                f"{status}={count}" for status, count in sorted(status_distribution.items())
            )
        )

        fields = (
            ("countAvailableUnits", "count_available_units"),
            ("availableUnitsToSell", "available_units_to_sell"),
            ("desiredUnitsToSell", "desired_units_to_sell"),
            ("countReservedUnits", "count_reserved_units"),
            ("countBlockedUnits", "count_blocked_units"),
            ("countPendingUnits", "count_pending_units"),
            ("countBlockingReservations", "count_blocking_reservations"),
        )
        for label, attribute in fields:
            values = [value for day in days if (value := getattr(day, attribute)) is not None]
            range_text = f"{min(values)}..{max(values)}" if values else "not-present"
            positive = sum(value > 0 for value in values)
            self.stdout.write(
                f"  {label}: days={len(values)} range={range_text} available-by-field={positive}"
            )

        self.stdout.write(f"  Selected strategy: {selected_strategy}")
        if request:
            by_date = {day.date: day for day in days}
            stay_days = [
                by_date.get(request.check_in + timedelta(days=offset))
                for offset in range((request.check_out - request.check_in).days)
            ]
            stay_strategies = {
                resolve_day_inventory(day).strategy for day in stay_days if day is not None
            }
            selected_stay_strategy = (
                next(iter(stay_strategies)) if len(stay_strategies) == 1 else "mixed"
            )
            self.stdout.write(f"  Selected stay strategy: {selected_stay_strategy}")
        self.stdout.write(
            f"  Available by selected strategy: "
            f"{sum(decision.is_available for decision in decisions)}"
        )
        self.stdout.write(
            f"  Inventory conflicts: {sum(decision.has_conflict for decision in decisions)}"
        )
        self.stdout.write(
            "  Reservation resources field received: "
            f"{'yes' if document.has_reservation_resources else 'no'}"
        )
        self.stdout.write(
            f"  Price zero or null days: "
            f"{sum(day.price is None or day.price == Decimal('0') for day in days)}"
        )
        diagnosis = "not_applicable" if stay_is_available else self._diagnose_no_availability(days)
        self.stdout.write(f"  No-availability diagnosis: {diagnosis}")

    @staticmethod
    def _diagnose_no_availability(days: tuple[CalendarDay, ...]) -> str:
        if not days:
            return "missing_calendar_data"
        ordered_dates = sorted(day.date for day in days)
        if any(
            (current - previous).days != 1
            for previous, current in zip(ordered_dates, ordered_dates[1:], strict=False)
        ):
            return "missing_calendar_data"

        decisions = [resolve_day_inventory(day) for day in days]
        if any(decision.has_conflict for decision in decisions):
            return "inventory_conflict"
        inventory_type = classify_inventory(days)
        if inventory_type == "single_unit" and all(day.is_available is not True for day in days):
            return "isAvailable_zero"
        if inventory_type == "multi_unit" and not any(
            decision.is_available for decision in decisions
        ):
            if any((day.count_blocked_units or 0) > 0 for day in days):
                return "blocked_inventory"
            sellable_values = [
                value
                for day in days
                for value in (
                    day.available_units_to_sell,
                    day.count_available_units,
                    day.desired_units_to_sell,
                )
                if value is not None
            ]
            if sellable_values and max(sellable_values) == 0:
                return "no_inventory_to_sell"
        if all(day.price is None or day.price == Decimal("0") for day in days):
            return "rate_missing"
        return "unknown"

    @staticmethod
    def _database_counts() -> tuple[int, int, int]:
        from apps.properties.models import PropertyImage
        from apps.reviews.models import Review

        return (
            Property.objects.count(),
            PropertyImage.objects.count(),
            Review.objects.count(),
        )
