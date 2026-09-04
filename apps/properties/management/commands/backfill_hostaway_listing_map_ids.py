"""Discover Listing Map IDs from Hostaway's own reservation records.

Hostaway's ``/listings`` payload does not carry ``listingMapId``, yet creating a
reservation is refused without it. Every reservation Hostaway holds does carry
one, so this reads them through the PII-free observation projection and stores
the value the account itself reports, never a guess.
"""

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import Property

VERIFICATION_SOURCE = "backfilled_reservation_listingMapId"


class Command(BaseCommand):
    help = "Store the Listing Map ID each listing's own reservations report."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be stored without writing anything.",
        )
        parser.add_argument(
            "--sample",
            type=int,
            default=20,
            help="Reservations to read per listing (1-100).",
        )
        parser.add_argument("--timeout", type=float, default=30.0)

    def handle(self, *args: object, **options: object) -> None:
        sample = int(options["sample"])
        if not 1 <= sample <= 100:
            raise CommandError("--sample must be between 1 and 100.")
        dry_run = bool(options["dry_run"])

        pending = list(
            Property.objects.filter(hostaway_listing_map_id__isnull=True)
            .order_by("hostaway_listing_id")
            .values_list("pk", "hostaway_listing_id")
        )
        if not pending:
            self.stdout.write(self.style.SUCCESS("Every property already has a Listing Map ID."))
            return

        stored = 0
        skipped: list[str] = []
        try:
            with HostawayClient(timeout=float(options["timeout"])) as client:
                for property_pk, listing_id in pending:
                    document = client.retrieve_reservation_observations(
                        listing_id=listing_id,
                        limit=sample,
                    )
                    observed = {
                        item.listing_map_id
                        for item in document.observations
                        if item.listing_map_id
                    }
                    if not observed:
                        skipped.append(f"{listing_id}: no reservation reported a Listing Map ID")
                        continue
                    if len(observed) > 1:
                        # Ambiguity means a multi-unit listing; a guess could file a
                        # booking against the wrong unit, so leave it for a human.
                        values = ", ".join(str(value) for value in sorted(observed))
                        skipped.append(f"{listing_id}: conflicting values ({values})")
                        continue
                    listing_map_id = observed.pop()
                    if dry_run:
                        self.stdout.write(f"{listing_id} -> {listing_map_id} (dry run)")
                        stored += 1
                        continue
                    if self._store(property_pk, listing_id, listing_map_id, skipped):
                        stored += 1
                        self.stdout.write(f"{listing_id} -> {listing_map_id}")
        except HostawayError as error:
            raise CommandError(f"Hostaway read failed: {error}") from error

        for line in skipped:
            self.stdout.write(self.style.WARNING(line))
        verb = "would be stored" if dry_run else "stored"
        self.stdout.write(
            self.style.SUCCESS(f"{stored} Listing Map ID(s) {verb}; {len(skipped)} skipped.")
        )

    def _store(
        self,
        property_pk: object,
        listing_id: int,
        listing_map_id: int,
        skipped: list[str],
    ) -> bool:
        with transaction.atomic():
            property_obj = Property.objects.select_for_update().get(pk=property_pk)
            if property_obj.hostaway_listing_map_id is not None:
                skipped.append(f"{listing_id}: already set while this command was running")
                return False
            taken = (
                Property.objects.exclude(pk=property_obj.pk)
                .filter(hostaway_listing_map_id=listing_map_id)
                .exists()
            )
            if taken:
                skipped.append(f"{listing_id}: {listing_map_id} belongs to another property")
                return False
            property_obj.hostaway_listing_map_id = listing_map_id
            property_obj.hostaway_listing_map_id_verified_at = timezone.now()
            property_obj.hostaway_listing_map_id_verification_source = VERIFICATION_SOURCE
            property_obj.save(
                update_fields=[
                    "hostaway_listing_map_id",
                    "hostaway_listing_map_id_verified_at",
                    "hostaway_listing_map_id_verification_source",
                    "updated_at",
                ]
            )
        return True
