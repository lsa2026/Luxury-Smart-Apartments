import argparse
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.hostaway.exceptions import HostawayError
from apps.integrations.hostaway.property_services import sync_properties


class Command(BaseCommand):
    help = "Synchronize Hostaway properties and their embedded resources."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing-id", type=_positive_integer)
        parser.add_argument(
            "--include-images",
            action=argparse.BooleanOptionalAction,
            default=True,
        )
        parser.add_argument(
            "--include-amenities",
            action=argparse.BooleanOptionalAction,
            default=True,
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--limit", type=_positive_integer)

    def handle(self, *args: object, **options: Any) -> None:
        try:
            report = sync_properties(
                listing_id=options["listing_id"],
                include_images=options["include_images"],
                include_amenities=options["include_amenities"],
                dry_run=options["dry_run"],
                force=options["force"],
                limit=options["limit"],
            )
        except HostawayError as exc:
            raise CommandError(f"Hostaway property sync failed: {exc}") from exc

        mode = " (dry run)" if options["dry_run"] else ""
        summary = (
            f"Hostaway properties sync{mode}: fetched={report.fetched}, "
            f"properties_created={report.properties_created}, "
            f"properties_updated={report.properties_updated}, "
            f"properties_failed={report.properties_failed}, "
            f"images_created={report.images_created}, "
            f"images_updated={report.images_updated}, "
            f"images_deactivated={report.images_deactivated}, "
            f"amenities_created={report.amenities_created}, "
            f"amenities_linked={report.amenities_linked}, "
            f"skipped={report.skipped}, errors={len(report.errors)}"
        )
        successful = report.properties_created + report.properties_updated
        if report.properties_failed and not successful:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))
        for error in report.errors[:10]:
            self.stderr.write(self.style.WARNING(error))


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer.")
    return parsed
