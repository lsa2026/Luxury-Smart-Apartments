"""Report configuration presence only; never read payments or send messages."""

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.payments.team_notices import enabled


class Command(BaseCommand):
    help = "Check team payment notice configuration without dispatching anything."

    def handle(self, *args, **options):
        provider = bool(
            settings.ULTRAMSG_ENABLED and settings.ULTRAMSG_INSTANCE_ID and settings.ULTRAMSG_TOKEN
        )
        self.stdout.write(
            f"Team payment notices: enabled={enabled()}; provider_configured={provider}; "
            "destination=existing_work_group; no dispatch."
        )
