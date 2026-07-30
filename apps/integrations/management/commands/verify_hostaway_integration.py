"""Management command for privacy-safe, read-only Hostaway verification."""

import argparse
import math
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.integrations.hostaway.verification import VerificationReport, verify_hostaway


class Command(BaseCommand):
    help = "Verify the live Hostaway read contract without writing to the database."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing-limit", type=_listing_limit, default=3)
        parser.add_argument("--listing-id", type=_positive_integer)
        parser.add_argument("--include-reviews", action="store_true")
        parser.add_argument("--include-amenities", action="store_true")
        parser.add_argument("--show-schema", action="store_true")
        parser.add_argument("--strict", action="store_true")
        parser.add_argument("--timeout", type=_positive_float, default=20.0)

    def handle(self, *args: object, **options: Any) -> None:
        try:
            with HostawayClient(timeout=options["timeout"]) as client:
                report = verify_hostaway(
                    client=client,
                    listing_limit=options["listing_limit"],
                    listing_id=options["listing_id"],
                    include_reviews=options["include_reviews"],
                    include_amenities=options["include_amenities"],
                )
        except HostawayError as exc:
            raise CommandError(f"Hostaway verification failed: {exc}") from exc

        self._print_report(report, show_schema=options["show_schema"])
        if options["strict"] and report.strict_errors:
            raise CommandError(
                f"Strict verification failed with {len(report.strict_errors)} error(s)."
            )

    def _print_report(
        self,
        report: VerificationReport,
        *,
        show_schema: bool,
    ) -> None:
        self.stdout.write(self.style.SUCCESS("Connection: succeeded"))
        self.stdout.write(f"Account: {report.masked_account_id}")
        self.stdout.write(f"Authentication duration: {report.authentication_seconds:.3f} seconds")
        self.stdout.write(f"Listings inspected: {len(report.listings)}")
        self.stdout.write(f"Response fields present: {_names(report.root_present_fields)}")
        self.stdout.write(f"Response fields missing: {_names(report.root_missing_fields)}")
        self.stdout.write(f"New response fields: {_names(report.root_unknown_fields)}")
        self.stdout.write(f"Response schema: {_schema(report.root_field_types)}")

        for index, listing in enumerate(report.listings, start=1):
            label = listing.identifier if listing.identifier is not None else "invalid"
            self.stdout.write(f"Listing {index} [{label}]")
            self.stdout.write(f"  Expected fields present: {_names(listing.present_fields)}")
            self.stdout.write(f"  Expected fields missing: {_names(listing.missing_fields)}")
            self.stdout.write(f"  New fields: {_names(listing.unknown_fields)}")
            self.stdout.write(f"  Type mismatches: {_names(listing.type_mismatches)}")
            self.stdout.write(f"  Listing schema: {_schema(listing.field_types)}")
            self.stdout.write(
                "  Images: "
                f"count={listing.image_count}, domains={_names(listing.image_domains)}, "
                f"missing_id={listing.images_missing_id}, "
                f"missing_url={listing.images_missing_url}, "
                f"non_https={listing.non_https_images}, "
                f"duplicates={listing.duplicate_images}, "
                f"signed_or_temporary={listing.temporary_image_urls}"
            )
            caption_fields = sorted(
                name for name in listing.image_fields if "caption" in name.casefold()
            )
            self.stdout.write(
                f"  Image ordering field: "
                f"{'sortOrder' if 'sortOrder' in listing.image_fields else 'not observed'}"
            )
            self.stdout.write(f"  Image caption fields: {_names(caption_fields)}")
            self.stdout.write(
                f"  Embedded amenities: count={listing.amenity_count}, "
                f"fields={_names(sorted(listing.amenity_fields))}"
            )
            if listing.validation_errors:
                self.stdout.write(f"  DTO validation notes: {len(listing.validation_errors)}")
            if show_schema:
                self.stdout.write(f"  Image schema: {_schema(listing.image_fields)}")
                self.stdout.write(f"  Embedded amenity schema: {_schema(listing.amenity_fields)}")

        if report.amenities_checked:
            self.stdout.write(
                f"Amenity endpoint: count={report.amenity_definition_count}, "
                f"fields={_names(sorted(report.amenity_definition_fields))}"
            )
            if show_schema:
                self.stdout.write(f"Amenity schema: {_schema(report.amenity_definition_fields)}")

        if report.reviews is not None:
            reviews = report.reviews
            rating_range = (
                f"{reviews.rating_min}..{reviews.rating_max}"
                if reviews.rating_min is not None and reviews.rating_max is not None
                else "not observed"
            )
            self.stdout.write(
                "Reviews: "
                f"count={reviews.fetched}, rating_range={rating_range}, "
                f"listing_matches={reviews.matched_listing_ids}, "
                f"listing_mismatches={reviews.unmatched_listing_ids}"
            )
            self.stdout.write(
                f"Review fields (privacy-filtered): {_names(sorted(reviews.field_types))}"
            )
            if show_schema:
                self.stdout.write(
                    f"Review schema (privacy-filtered): {_schema(reviews.field_types)}"
                )

        self.stdout.write("Recommendations:")
        for recommendation in report.recommendations:
            self.stdout.write(f"  - {recommendation}")


def _names(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _schema(values: dict[str, str]) -> str:
    return ", ".join(f"{name}:{field_type}" for name, field_type in values.items()) or "none"


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer.")
    return parsed


def _listing_limit(value: str) -> int:
    parsed = _positive_integer(value)
    if parsed > 100:
        raise argparse.ArgumentTypeError("Listing limit cannot exceed 100.")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("Timeout must be positive.")
    return parsed
