import argparse
from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.hostaway.exceptions import HostawayError
from apps.integrations.hostaway.services import sync_reviews


class Command(BaseCommand):
    help = "Synchronize published guest-to-host reviews from Hostaway."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing-id", type=int)
        parser.add_argument("--departure-from", type=_iso_date)
        parser.add_argument("--departure-to", type=_iso_date)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: Any) -> None:
        try:
            report = sync_reviews(
                listing_id=options["listing_id"],
                departure_from=options["departure_from"],
                departure_to=options["departure_to"],
                dry_run=options["dry_run"],
            )
        except HostawayError as exc:
            raise CommandError(f"Hostaway review sync failed: {exc}") from exc

        mode = " (dry run)" if options["dry_run"] else ""
        summary = (
            f"Hostaway reviews sync{mode}: "
            f"fetched={report.fetched}, created={report.created}, "
            f"updated={report.updated}, skipped={report.skipped}, failed={report.failed}, "
            f"matched_by_listing_map_id={report.match_strategies['listing_map_id']}, "
            f"matched_by_listing_id_fallback="
            f"{report.match_strategies['listing_id_fallback']}, "
            f"unmatched={report.match_strategies['unmatched']}"
        )
        if report.failed and not (report.created or report.updated or report.skipped):
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Date must use YYYY-MM-DD format.") from exc
