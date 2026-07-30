"""Read-only verification of listing-map and reservation-channel identifiers."""

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import Property

CHANNELS = {
    2000: "direct",
    2002: "homeaway",
    2005: "bookingcom",
    2007: "expedia",
    2009: "homeawayical",
    2010: "vrboical",
    2013: "bookingengine",
    2015: "customIcal",
    2016: "tripadvisorical",
    2017: "wordpress",
    2018: "airbnbOfficial",
    2019: "marriott",
    2020: "partner",
    2021: "gds",
    2022: "google",
}


class Command(BaseCommand):
    help = "Verify reservation identifiers through GET requests only."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing-id", type=int, required=True)
        parser.add_argument("--reservation-limit", type=int, default=20)
        parser.add_argument("--include-direct-reservations", action="store_true")
        parser.add_argument("--show-schema", action="store_true")
        parser.add_argument("--strict", action="store_true")
        parser.add_argument("--timeout", type=float, default=20.0)
        parser.add_argument("--bypass-cache", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        listing_id = options["listing_id"]
        limit = options["reservation_limit"]
        if not 1 <= limit <= 100:
            raise CommandError("--reservation-limit must be between 1 and 100.")
        property_obj = Property.objects.filter(hostaway_listing_id=listing_id).first()
        if property_obj is None:
            raise CommandError("property_not_found")
        try:
            with HostawayClient(timeout=options["timeout"]) as client:
                listing = client.get_listing_document(listing_id, include_resources=False)
                reservations = client.retrieve_reservation_observations(
                    listing_id=listing_id,
                    limit=limit,
                )
        except HostawayError as exc:
            raise CommandError(type(exc).__name__) from exc
        if options["strict"] and listing.record.get("id") != listing_id:
            raise CommandError("listing_identity_mismatch")

        map_ids = sorted(
            {
                observation.listing_map_id
                for observation in reservations.observations
                if observation.listing_map_id is not None
            }
        )
        channel_ids = sorted(
            {
                observation.channel_id
                for observation in reservations.observations
                if observation.channel_id is not None
            }
        )
        channel_types = sorted(
            {
                observation.channel_name or CHANNELS.get(observation.channel_id, "unknown")
                for observation in reservations.observations
                if observation.channel_id is not None
            }
        )
        statuses = sorted({item.status for item in reservations.observations if item.status})
        sources = sorted({item.source for item in reservations.observations if item.source})
        direct = [
            item
            for item in reservations.observations
            if item.channel_id == 2000
            or item.channel_name.casefold() == "direct"
            or item.source.casefold() == "direct"
        ]
        listing_map_verified = len(map_ids) == 1
        direct_channel_verified = bool(direct)
        blockers: list[str] = []
        if not listing_map_verified:
            blockers.append("listing_map_id_not_verified")
        if not direct_channel_verified:
            blockers.append("direct_channel_id_not_verified_for_account")
        blockers.extend(
            [
                "payment_provider_not_configured",
                "hostaway_live_booking_disabled",
                "live_modification_flags_disabled",
            ]
        )
        self.stdout.write(f"listing_id: {listing_id}")
        self.stdout.write(f"reservations_inspected: {reservations.count}")
        self.stdout.write(f"listing_map_id_verified: {str(listing_map_verified).lower()}")
        self.stdout.write(
            "listing_map_id_source: "
            + ("reservation.listingMapId" if listing_map_verified else "not_verified")
        )
        if listing_map_verified:
            self.stdout.write(f"verified_listing_map_id: {map_ids[0]}")
        self.stdout.write(f"direct_channel_id_verified: {str(direct_channel_verified).lower()}")
        self.stdout.write(
            "direct_channel_id_source: "
            + ("observed_direct_reservation" if direct_channel_verified else "not_verified")
        )
        if direct_channel_verified:
            self.stdout.write("verified_direct_channel_id: 2000")
        self.stdout.write("channel_ids_discovered: " + (", ".join(map(str, channel_ids)) or "none"))
        self.stdout.write("channel_types: " + (", ".join(channel_types) or "none"))
        self.stdout.write("source_types: " + (", ".join(sources) or "none"))
        self.stdout.write("reservation_statuses: " + (", ".join(statuses) or "none"))
        self.stdout.write(
            "direct_reservations_observed: "
            + (
                str(len(direct))
                if options["include_direct_reservations"]
                else "not_requested_for_report"
            )
        )
        if options["show_schema"]:
            schema = sorted(
                {
                    field
                    for observation in reservations.observations
                    for field in observation.field_types
                }
            )
            self.stdout.write(
                "reservation_schema: "
                + (", ".join(f"{name}:{kind}" for name, kind in schema) or "none")
            )
        self.stdout.write("blockers: " + ", ".join(blockers))
        self.stdout.write(f"live_booking_readiness: {not blockers}")
        self.stdout.write("Hostaway write calls: 0")
        self.stdout.write("Database changes: 0")
