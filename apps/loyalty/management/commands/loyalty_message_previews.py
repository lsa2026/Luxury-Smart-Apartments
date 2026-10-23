import json

from django.core.management.base import BaseCommand

from apps.loyalty.message_previews import catalogue, send_catalogue


class Command(BaseCommand):
    help = "Owner-only synthetic WhatsApp previews; dry run unless --send is explicit."

    def add_arguments(self, parser):
        parser.add_argument("--language", choices=["ar", "en", "fr"], default="ar")
        parser.add_argument("--batch", required=True)
        parser.add_argument("--send", action="store_true")
        parser.add_argument("--kind", choices=["booking-invitation", "welcome"])

    def handle(self, *args, **options):
        if options["send"]:
            result = send_catalogue(
                batch=options["batch"], language=options["language"], kind=options["kind"]
            )
        else:
            result = [(key, "dry_run") for key, _, _ in catalogue(options["language"])]
        self.stdout.write(json.dumps(result))
