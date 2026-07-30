"""Persist identifiers that were verified from a documented read-only source."""

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.properties.models import Property


class Command(BaseCommand):
    help = "Store one explicitly verified Hostaway Listing Map ID without inference."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing-id", type=int, required=True)
        parser.add_argument("--listing-map-id", type=int, required=True)
        parser.add_argument("--source", default="verified_reservation_listingMapId")

    def handle(self, *args: object, **options: object) -> None:
        listing_id = options["listing_id"]
        listing_map_id = options["listing_map_id"]
        source = str(options["source"]).strip()
        if listing_id <= 0 or listing_map_id <= 0:
            raise CommandError("Both identifiers must be positive integers.")
        if not source or len(source) > 100:
            raise CommandError("Verification source must contain 1 to 100 characters.")

        with transaction.atomic():
            matches = list(
                Property.objects.select_for_update().filter(hostaway_listing_id=listing_id)[:2]
            )
            if len(matches) != 1:
                raise CommandError("Exactly one local Property must match the Listing ID.")
            property_obj = matches[0]
            if (
                property_obj.hostaway_listing_map_id is not None
                and property_obj.hostaway_listing_map_id != listing_map_id
            ):
                raise CommandError("The existing Listing Map ID differs; no value was changed.")
            duplicate = (
                Property.objects.exclude(pk=property_obj.pk)
                .filter(hostaway_listing_map_id=listing_map_id)
                .exists()
            )
            if duplicate:
                raise CommandError("The verified Listing Map ID belongs to another Property.")
            property_obj.hostaway_listing_map_id = listing_map_id
            property_obj.hostaway_listing_map_id_verified_at = timezone.now()
            property_obj.hostaway_listing_map_id_verification_source = source
            property_obj.save(
                update_fields=[
                    "hostaway_listing_map_id",
                    "hostaway_listing_map_id_verified_at",
                    "hostaway_listing_map_id_verification_source",
                    "updated_at",
                ]
            )
        self.stdout.write(self.style.SUCCESS("Verified Hostaway identifiers stored safely."))
