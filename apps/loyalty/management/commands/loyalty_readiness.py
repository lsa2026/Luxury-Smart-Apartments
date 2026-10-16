"""SELECT-only preflight. Never creates members, sends mail, or settles a booking."""

import json
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.loyalty.launch import (
    booking_allowed,
    historical_award_allowed,
    pilot_emails,
    pilot_only,
    pilot_references,
    stay_exception_allowed,
)
from apps.loyalty.services import launch_at
from apps.loyalty.site_adapter import booking_facts
from apps.loyalty.views import approved_terms
from apps.reservations.models import Reservation


class Command(BaseCommand):
    help = "Read-only loyalty launch checks, with no personal data in the result."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--reservation", default="")
        group.add_argument("--reservation-id", default="")

    def handle(self, *args, **options):
        try:
            launch_at()
            launch_configured = True
        except ValidationError:
            launch_configured = False
        gates = {
            "programme_enabled": settings.LOYALTY_ENABLED,
            "interface_enabled": settings.LOYALTY_UI_ENABLED,
            "launch_date_configured": launch_configured,
            "terms_reference_configured": bool(approved_terms()),
            "pilot_only": pilot_only(),
            "pilot_accounts_configured": bool(pilot_emails()),
            "pilot_bookings_configured": bool(pilot_references()),
            "automatic_loyalty_messages_suppressed": pilot_only()
            or not settings.LOYALTY_MESSAGES_ENABLED,
        }
        result = {"read_only": True, "gates": gates}
        if options["reservation"] or options["reservation_id"]:
            lookup = {"public_reference": options["reservation"]}
            if options["reservation_id"]:
                try:
                    lookup = {"pk": UUID(options["reservation_id"])}
                except (ValueError, TypeError, AttributeError) as exc:
                    raise CommandError("A valid reservation identifier is required.") from exc
            booking = (
                Reservation.objects.select_related(
                    "booking_intent__customer", "booking_intent__quote", "property"
                )
                .filter(**lookup)
                .first()
            )
            result["booking_found"] = booking is not None
            if booking:
                reason = "launch_date_required"
                if launch_configured:
                    _, reason = booking_facts(booking)
                result["linked_customer_recorded"] = bool(
                    booking.booking_intent_id and booking.booking_intent.customer_id
                )
                result["arrival_recorded"] = bool(booking.loyalty_state.get("arrival"))
                result["booking_in_scope"] = booking_allowed(booking)
                result["historical_acceptance_exception"] = historical_award_allowed(booking)
                result["test_only_stay_exception"] = stay_exception_allowed(booking)
                result["award_evidence_ready"] = not reason
                result["blocker"] = reason
        # Configuring a reference is not a legal opinion, consent, or proof of
        # publication. This command cannot approve terms or authorize launch.
        self.stdout.write(json.dumps(result, sort_keys=True))
