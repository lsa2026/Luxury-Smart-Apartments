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

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import Property

DEFAULT_WINDOW_DAYS = 30


class Command(BaseCommand):
    help = "Refresh each property's indicative nightly rate from its Hostaway calendar."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing-id", type=int)
        parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: Any) -> None:
        window_days = options["days"]
        if window_days < 1:
            self.stderr.write("--days must be at least 1.")
            return

        properties = Property.objects.filter(hostaway_is_active=True).order_by("id")
        if options["listing_id"]:
            properties = properties.filter(hostaway_listing_id=options["listing_id"])
        property_rows = list(properties)

        start = timezone.localdate()
        end = start + timedelta(days=window_days)
        examined = updated = unchanged = no_availability = failed = 0
        candidates: list[tuple[Property, Decimal, str]] = []

        with HostawayClient() as client:
            for property_obj in property_rows:
                examined += 1
                try:
                    lowest = self._lowest_available_night(
                        client,
                        property_obj.hostaway_listing_id,
                        start=start,
                        end=end,
                    )
                except (HostawayError, ValueError) as exc:
                    # A single unreachable listing must not blank its anchor.
                    failed += 1
                    self.stderr.write(
                        f"listing {property_obj.hostaway_listing_id}: {type(exc).__name__}"
                    )
                    continue

                if lowest is None:
                    no_availability += 1
                    continue
                currency = (property_obj.currency_code or "").strip().upper()
                if len(currency) != 3 or not currency.isalpha():
                    failed += 1
                    self.stderr.write(
                        f"listing {property_obj.hostaway_listing_id}: invalid currency"
                    )
                    continue
                if (
                    lowest == property_obj.indicative_nightly_from
                    and currency == property_obj.indicative_currency
                ):
                    unchanged += 1
                    continue
                candidates.append((property_obj, lowest, currency))

        updated = len(candidates)
        aborted = failed > 0 and not options["dry_run"]
        if not options["dry_run"] and not aborted and candidates:
            priced_at = timezone.now()
            for property_obj, lowest, currency in candidates:
                property_obj.indicative_nightly_from = lowest
                property_obj.indicative_currency = currency
                property_obj.indicative_priced_at = priced_at
            with transaction.atomic():
                Property.objects.bulk_update(
                    [property_obj for property_obj, _lowest, _currency in candidates],
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
        client: HostawayClient,
        listing_id: int,
        *,
        start: date,
        end: date,
    ) -> Decimal | None:
        """The smallest price Hostaway reports for a bookable day in the window."""
        document = client.get_listing_calendar(
            listing_id,
            start_date=start,
            end_date=end,
        )
        prices = [
            day.price
            for day in document.days
            # Only a day Hostaway calls available can anchor a price. An unknown
            # availability is not treated as bookable.
            if day.is_available is True and day.price is not None and day.price > 0
        ]
        return min(prices) if prices else None
