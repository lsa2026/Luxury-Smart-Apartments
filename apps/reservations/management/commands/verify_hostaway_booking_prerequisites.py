"""Read-only verification of live-booking prerequisites."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import Property


class Command(BaseCommand):
    help = "Verify booking prerequisites without creating a reservation."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing-id", type=int, required=True)
        parser.add_argument("--show-schema", action="store_true")
        parser.add_argument("--strict", action="store_true")
        parser.add_argument("--timeout", type=float, default=20.0)

    def handle(self, *args: object, **options: object) -> None:
        listing_id = options["listing_id"]
        property_obj = Property.objects.filter(hostaway_listing_id=listing_id).first()
        if property_obj is None:
            raise CommandError("property_not_found")
        try:
            with HostawayClient(timeout=options["timeout"]) as client:
                document = client.get_listing_document(
                    listing_id,
                    include_resources=False,
                )
        except HostawayError as exc:
            raise CommandError(type(exc).__name__) from exc
        record_id = document.record.get("id")
        if options["strict"] and record_id != listing_id:
            raise CommandError("listing_identity_mismatch")
        blockers: list[str] = []
        if property_obj.hostaway_listing_map_id is None:
            blockers.append("listing_map_id_not_verified")
        if settings.HOSTAWAY_DIRECT_CHANNEL_ID is None:
            blockers.append("direct_channel_id_not_configured")
        if not settings.HOSTAWAY_LIVE_BOOKING_ENABLED:
            blockers.append("hostaway_live_booking_disabled")
        blockers.append("payment_provider_not_configured")
        self.stdout.write("Connection: succeeded")
        self.stdout.write(f"Listing verified: {record_id == listing_id}")
        self.stdout.write(
            "Listing Map ID: "
            + ("verified" if property_obj.hostaway_listing_map_id else "not verified")
        )
        self.stdout.write(
            "Direct Channel ID: "
            + ("configured" if settings.HOSTAWAY_DIRECT_CHANNEL_ID else "not configured")
        )
        self.stdout.write(f"Live booking enabled: {settings.HOSTAWAY_LIVE_BOOKING_ENABLED}")
        self.stdout.write("Payment provider: not configured")
        self.stdout.write("Ready for live booking: False")
        self.stdout.write("Blockers: " + ", ".join(blockers))
        if options["show_schema"]:
            schema = sorted(
                f"{key}:{type(value).__name__}" for key, value in document.record.items()
            )
            self.stdout.write("Listing schema: " + ", ".join(schema))
        self.stdout.write("Reservation POST calls: 0")
