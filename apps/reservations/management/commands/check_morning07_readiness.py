"""Validate Morning07 deployment configuration without reading guests or sending messages."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check the enabled morning booking bridge without external writes."

    def handle(self, *args, **options):
        if not settings.MORNING07_N8N_ENABLED:
            self.stdout.write("Morning07: disabled; no dispatch.")
            return
        required = {
            "dedicated_webhook": settings.MORNING07_N8N_WEBHOOK_URL
            == "https://lsa25.app.n8n.cloud/webhook/lsa07-morning-stay-v1",
            "whatsapp_enabled": settings.ULTRAMSG_ENABLED,
            "whatsapp_instance_configured": bool(settings.ULTRAMSG_INSTANCE_ID),
            "whatsapp_token_configured": bool(settings.ULTRAMSG_TOKEN),
            "booking_queue_configured": bool(settings.CELERY_BROKER_URL),
        }
        missing = [name for name, present in required.items() if not present]
        if missing:
            raise CommandError("Morning07 configuration incomplete: " + ", ".join(missing))
        self.stdout.write(
            "Morning07: ready; event-only dispatch; "
            "WhatsApp configuration and booking queue present."
        )
