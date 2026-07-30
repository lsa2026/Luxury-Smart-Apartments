"""Read-only verification of future reservation modification prerequisites."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.properties.models import Property
from apps.reservations.models import Reservation


class Command(BaseCommand):
    help = "Verify future modification prerequisites without writing to Hostaway."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing-id", type=int, required=True)
        parser.add_argument("--reservation-id", type=int)
        parser.add_argument("--strict", action="store_true")
        parser.add_argument("--show-schema", action="store_true")
        parser.add_argument("--timeout", type=float, default=20.0)

    def handle(self, *args: object, **options: object) -> None:
        listing_id = options["listing_id"]
        property_obj = Property.objects.filter(hostaway_listing_id=listing_id).first()
        if property_obj is None:
            raise CommandError("property_not_found")
        snapshot = None
        try:
            with HostawayClient(timeout=options["timeout"]) as client:
                listing = client.get_listing_document(listing_id, include_resources=False)
                if options["reservation_id"]:
                    snapshot = client.get_reservation(options["reservation_id"])
        except HostawayError as exc:
            raise CommandError(type(exc).__name__) from exc
        if options["strict"] and listing.record.get("id") != listing_id:
            raise CommandError("listing_identity_mismatch")
        local_confirmed = Reservation.objects.filter(
            property=property_obj,
            normalized_status=Reservation.Status.CONFIRMED,
        ).exists()
        blockers = []
        if property_obj.hostaway_listing_map_id is None:
            blockers.append("listing_map_id_not_verified")
        if settings.HOSTAWAY_DIRECT_CHANNEL_ID is None:
            blockers.append("direct_channel_id_not_configured")
        if not local_confirmed:
            blockers.append("confirmed_local_reservation_required")
        blockers.extend(
            [
                "payment_provider_not_configured",
                "refund_workflow_not_configured",
            ]
        )
        if not settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED:
            blockers.append("hostaway_live_modification_disabled")
        if not settings.HOSTAWAY_LIVE_EXTENSION_ENABLED:
            blockers.append("hostaway_live_extension_disabled")
        if not settings.HOSTAWAY_LIVE_CANCELLATION_ENABLED:
            blockers.append("hostaway_live_cancellation_disabled")
        self.stdout.write(f"listing_verified: {listing.record.get('id') == listing_id}")
        self.stdout.write(f"reservation_checked: {snapshot is not None}")
        if snapshot is not None:
            self.stdout.write(f"reservation_id: ****{str(snapshot.reservation_id)[-4:]}")
            self.stdout.write(f"reservation_status: {snapshot.status}")
            self.stdout.write(f"reservation_channel_id: {snapshot.channel_id or 'missing'}")
        self.stdout.write("documented_update_method: PUT /reservations/{reservationId}")
        self.stdout.write(
            "documented_update_fields: listingMapId, arrivalDate, departureDate, "
            "numberOfGuests, totalPrice, currency, financeField"
        )
        self.stdout.write(
            "documented_cancellation_method: PUT /reservations/{reservationId}/statuses/cancelled"
        )
        self.stdout.write("documented_cancellation_fields: cancelledBy")
        if options["show_schema"]:
            self.stdout.write(
                "listing_schema: "
                + ", ".join(
                    sorted(f"{key}:{type(value).__name__}" for key, value in listing.record.items())
                )
            )
        self.stdout.write("blockers: " + ", ".join(blockers))
        self.stdout.write("Hostaway write calls: 0")
        self.stdout.write("Database changes: 0")
