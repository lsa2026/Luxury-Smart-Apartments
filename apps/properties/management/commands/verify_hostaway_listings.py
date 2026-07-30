"""Read-only, privacy-safe verification of every Hostaway listing."""

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError, HostawayResponseError
from apps.integrations.hostaway.listing_validators import normalize_listing
from apps.properties.models import Property


class Command(BaseCommand):
    help = "Inspect all Hostaway listings using offset pagination without database writes."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--page-size", type=int, default=100)
        parser.add_argument("--timeout", type=float, default=20.0)

    def handle(self, *args: object, **options: object) -> None:
        page_size = options["page_size"]
        if not 1 <= page_size <= 100:
            raise CommandError("--page-size must be between 1 and 100.")
        timeout = options["timeout"]
        if timeout <= 0 or timeout > 60:
            raise CommandError("--timeout must be between 0 and 60 seconds.")

        local_ids = set(Property.objects.values_list("hostaway_listing_id", flat=True))
        client = HostawayClient(timeout=timeout)
        offset = 0
        total = 0
        expected_count: int | None = None
        try:
            while True:
                page, count = client.get_listings(
                    limit=page_size,
                    offset=offset,
                    include_resources=True,
                )
                if count is not None:
                    if expected_count is None:
                        expected_count = count
                    elif count != expected_count:
                        raise CommandError("Hostaway listing count changed during verification.")
                for raw in page:
                    try:
                        listing = normalize_listing(raw)
                    except HostawayResponseError:
                        self.stderr.write("listing_schema=invalid")
                        continue
                    total += 1
                    basics_complete = bool(
                        listing.name
                        and listing.person_capacity
                        and listing.currency_code
                        and listing.images
                    )
                    self.stdout.write(
                        " | ".join(
                            [
                                f"listing_id={listing.listing_id}",
                                f"name={listing.name[:120]}",
                                f"special_status={listing.special_status or 'null'}",
                                f"images={len(listing.images)}",
                                f"amenities={len(listing.amenities)}",
                                f"city={listing.city[:80] or '—'}",
                                f"currency={listing.currency_code or '—'}",
                                f"capacity={listing.person_capacity or 0}",
                                f"exists_locally={listing.listing_id in local_ids}",
                                f"active={listing.is_active}",
                                f"basics_complete={basics_complete}",
                            ]
                        )
                    )
                offset += len(page)
                if not page:
                    break
                if expected_count is not None and offset >= expected_count:
                    break
                if expected_count is None and len(page) < page_size:
                    break
        except HostawayError as exc:
            raise CommandError(f"Read-only Hostaway listing verification failed: {exc}") from exc
        finally:
            client.close()
        self.stdout.write(self.style.SUCCESS(f"hostaway_listings_total={total}"))
        self.stdout.write("database_writes=0 hostaway_writes=0")
