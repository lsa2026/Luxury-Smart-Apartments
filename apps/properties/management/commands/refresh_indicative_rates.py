"""Store the cheapest available night per property, for display only.

A card cannot call Hostaway while a page renders, so the anchor has to be
precomputed. What is stored is the lowest price Hostaway itself reports for an
available day in the coming window: no arithmetic, no averaging, no tax added.
The figure is presented as "from", never as the price of a stay, because the
real total still comes from a live quote for the guest's own dates.

A listing whose window contains no available day keeps whatever it had; a run
that cannot reach Hostaway writes nothing at all, so a network failure never
blanks the anchors already on the site.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.integrations.hostaway.availability_validators import CalendarDocument
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import Property, PropertyPriceCalendar
from apps.reservations.services.availability import resolve_day_inventory

DEFAULT_WINDOW_DAYS = 30
DEFAULT_CALENDAR_DAYS = 365


class Command(BaseCommand):
    help = "Refresh each property's indicative nightly rate from its Hostaway calendar."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing-id", type=int)
        parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
        parser.add_argument("--calendar-days", type=int, default=DEFAULT_CALENDAR_DAYS)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--if-missing", action="store_true")

    def handle(self, *args: object, **options: Any) -> None:
        window_days = options["days"]
        calendar_days = options["calendar_days"]
        if not 1 <= window_days <= 365 or not window_days <= calendar_days <= 365:
            self.stderr.write("Require 1 <= --days <= --calendar-days <= 365.")
            return

        properties = Property.objects.filter(hostaway_is_active=True).order_by("id")
        if options["if_missing"]:
            properties = properties.filter(price_calendar__isnull=True)
        if options["listing_id"]:
            properties = properties.filter(hostaway_listing_id=options["listing_id"])
        property_rows = list(properties)
        if not property_rows:
            self.stdout.write("No price calendars need refreshing.")
            return

        start = timezone.localdate()
        end = start + timedelta(days=window_days)
        calendar_end = start + timedelta(days=calendar_days)
        examined = updated = unchanged = no_availability = failed = 0
        candidates: list[tuple[Property, Decimal | None, str, list[dict[str, object]]]] = []

        with HostawayClient() as client:
            for property_obj in property_rows:
                examined += 1
                try:
                    document = client.get_listing_calendar(
                        property_obj.hostaway_listing_id,
                        start_date=start,
                        end_date=calendar_end,
                    )
                    lowest = self._lowest_available_night(document, start=start, end=end)
                    days = self._public_days(document, start=start, end=calendar_end)
                except (HostawayError, ValueError) as exc:
                    # A single unreachable listing must not blank its anchor.
                    failed += 1
                    self.stderr.write(
                        f"listing {property_obj.hostaway_listing_id}: {type(exc).__name__}"
                    )
                    continue

                if lowest is None:
                    no_availability += 1
                currency = (
                    (property_obj.price_currency_override or property_obj.currency_code)
                    .strip()
                    .upper()
                )
                if len(currency) != 3 or not currency.isalpha():
                    failed += 1
                    self.stderr.write(
                        f"listing {property_obj.hostaway_listing_id}: invalid currency"
                    )
                    continue
                if lowest is not None and (
                    lowest == property_obj.indicative_nightly_from
                    and currency == property_obj.indicative_currency
                ):
                    unchanged += 1
                elif lowest is not None:
                    updated += 1
                candidates.append((property_obj, lowest, currency, days))

        aborted = failed > 0 and not options["dry_run"]
        if not options["dry_run"] and not aborted and candidates:
            priced_at = timezone.now()
            with transaction.atomic():
                anchors = []
                for property_obj, lowest, currency, days in candidates:
                    PropertyPriceCalendar.objects.update_or_create(
                        property_id=property_obj.pk,
                        defaults={
                            "currency": currency,
                            "start_date": start,
                            "end_date": calendar_end,
                            "days": days,
                            "fetched_at": priced_at,
                        },
                    )
                    if lowest is not None:
                        property_obj.indicative_nightly_from = lowest
                        property_obj.indicative_currency = currency
                        # Successful verification, even when the amount has not changed.
                        property_obj.indicative_priced_at = priced_at
                        anchors.append(property_obj)
                if anchors:
                    Property.objects.bulk_update(
                        anchors,
                        fields=(
                            "indicative_nightly_from",
                            "indicative_currency",
                            "indicative_priced_at",
                        ),
                    )

        mode = " (dry run)" if options["dry_run"] else ""
        result = " aborted=true" if aborted else ""
        self.stdout.write(
            f"Indicative rates{mode}: examined={examined}, updated={updated}, "
            f"unchanged={unchanged}, no_availability={no_availability}, failed={failed}"
            f"{result}"
        )

    def _lowest_available_night(
        self,
        document: CalendarDocument,
        *,
        start: date,
        end: date,
    ) -> Decimal | None:
        """The smallest price Hostaway reports for a bookable day in the window."""
        prices = [
            day.price
            for day in document.days
            # Only a day Hostaway calls available can anchor a price. An unknown
            # availability is not treated as bookable.
            if start <= day.date <= end
            and resolve_day_inventory(day).is_available
            and day.price is not None
            and day.price > 0
        ]
        return min(prices) if prices else None

    @staticmethod
    def _public_days(
        document: CalendarDocument, *, start: date, end: date
    ) -> list[dict[str, object]]:
        """Allowlisted facts only: no raw Hostaway payload, notes or guest data."""
        return [
            {
                "date": day.date.isoformat(),
                "price": format(day.price, "f")
                if day.price is not None and day.price > 0
                else None,
                "available": bool(resolve_day_inventory(day).is_available),
                "arrival_available": bool(
                    resolve_day_inventory(day).is_available and not day.closed_on_arrival
                ),
                "departure_available": not bool(day.closed_on_departure),
                "minimum_stay": day.minimum_stay,
            }
            for day in document.days
            if start <= day.date < end
        ]
