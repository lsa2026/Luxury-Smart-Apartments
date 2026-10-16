"""SELECT-only preflight. Never creates members, sends mail, or settles a booking."""

import json

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.loyalty.launch import (
    booking_allowed,
    historical_award_allowed,
    pilot_emails,
    pilot_only,
    pilot_references,
)
from apps.loyalty.site_adapter import booking_facts
from apps.loyalty.views import approved_terms
from apps.reservations.models import Reservation


class Command(BaseCommand):
    help = "Read-only loyalty launch checks, with no personal data in the result."

    def add_arguments(self, parser):
        parser.add_argument("--reservation", default="")

    def handle(self, *args, **options):
        gates = {
            "programme_enabled": settings.LOYALTY_ENABLED,
            "interface_enabled": settings.LOYALTY_UI_ENABLED,
            "terms_reference_configured": bool(approved_terms()),
            "pilot_only": pilot_only(),
            "pilot_accounts_configured": bool(pilot_emails()),
            "pilot_bookings_configured": bool(pilot_references()),
            "automatic_loyalty_messages_suppressed": pilot_only()
            or not settings.LOYALTY_MESSAGES_ENABLED,
        }
        result = {"read_only": True, "gates": gates}
        if options["reservation"]:
            booking = (
                Reservation.objects.select_related(
                    "booking_intent__customer", "booking_intent__quote", "property"
                )
                .filter(public_reference=options["reservation"])
                .first()
            )
            result["booking_found"] = booking is not None
            if booking:
                _, reason = booking_facts(booking)
                result["booking_in_scope"] = booking_allowed(booking)
                result["historical_acceptance_exception"] = historical_award_allowed(booking)
                result["award_evidence_ready"] = not reason
                result["blocker"] = reason
        # Configuring a reference is not a legal opinion, consent, or proof of
        # publication. This command cannot approve terms or authorize launch.
        self.stdout.write(json.dumps(result, sort_keys=True))
