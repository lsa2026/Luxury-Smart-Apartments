"""Configuration-only evidence and an additive-schema deployment gate, no guest reads."""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder


class Command(BaseCommand):
    help = "Check day-use configuration and optionally wait for the web migration."

    def add_arguments(self, parser):
        parser.add_argument("--wait-for-schema", action="store_true")

    def handle(self, *args, **options):
        if options["wait_for_schema"]:
            # The web service alone migrates. Keep the old worker alive while
            # the new worker build waits, so normal jobs never see a partial schema.
            deadline = time.monotonic() + 60
            expected = {
                "reservations_dayusestay",
                "reservations_dayusehostawayownership",
                "reservations_dayusecommunicationevent",
                "reservations_dayuse08paymentevent",
                "reservations_dayuse08paymentdelivery",
                "payments_dayusepaymentattempt",
            }

            def ready():
                if not expected.issubset(connection.introspection.table_names()):
                    return False
                return {
                    ("payments", "0020_dayusepaymentattempt_stay_and_more"),
                    ("reservations", "0031_dayusestay_dayusepaymentlinkdelivery_and_more"),
                }.issubset(MigrationRecorder(connection).applied_migrations())

            while not ready():
                if time.monotonic() >= deadline:
                    raise CommandError("DayUse08 schema not ready; deploy the web migration first.")
                time.sleep(3)
            self.stdout.write("DayUse08: additive schema present.")
        if not settings.DAY_USE08_ENABLED:
            self.stdout.write("DayUse08: disabled; no dispatch.")
            return
        required = {
            "whatsapp_enabled": settings.ULTRAMSG_ENABLED,
            "whatsapp_configured": bool(settings.ULTRAMSG_INSTANCE_ID and settings.ULTRAMSG_TOKEN),
            "queue_configured": bool(settings.CELERY_BROKER_URL),
            "hyperpay_enabled": settings.HYPERPAY_ENABLED,
            "hostaway_booking_enabled": settings.HOSTAWAY_LIVE_BOOKING_ENABLED,
            "direct_channel": settings.HOSTAWAY_DIRECT_CHANNEL_ID == 2000,
        }
        for suffix, actual in (
            ("booking", settings.DAY_USE08_WEBHOOK_URL),
            ("payment", settings.DAY_USE08_PAYMENT_WEBHOOK_URL),
            ("ownership", settings.DAY_USE08_OWNERSHIP_WEBHOOK_URL),
        ):
            required[suffix + "_webhook"] = (
                actual == f"https://lsa25.app.n8n.cloud/webhook/lsa08-day-use-{suffix}-v1"
            )
        missing = [name for name, present in required.items() if not present]
        if missing:
            raise CommandError("DayUse08 configuration incomplete: " + ", ".join(missing))
        self.stdout.write(
            "DayUse08: configured; operational rollout gates require separate evidence."
        )
