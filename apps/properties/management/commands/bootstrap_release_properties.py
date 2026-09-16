"""Load the reviewed property snapshot when a release database is brand new."""

from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.properties.models import Property


SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "release_properties.json"
EXPECTED_PROPERTY_COUNT = 7


class Command(BaseCommand):
    help = (
        "Load the reviewed property, image and amenity snapshot into an empty database."
    )

    def handle(self, *args: object, **options: object) -> None:
        existing_count = Property.objects.count()
        if existing_count:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Release property bootstrap skipped: {existing_count} properties already exist."
                )
            )
            return

        if not SEED_PATH.is_file():
            raise CommandError(f"Release property snapshot is missing: {SEED_PATH}")

        # The snapshot is created from the reviewed local release database.  It
        # contains no provider credentials and lets a new Render database boot
        # with the guest catalogue before Hostaway integration is enabled.
        call_command("loaddata", str(SEED_PATH), verbosity=options.get("verbosity", 1))

        imported_count = Property.objects.count()
        if imported_count != EXPECTED_PROPERTY_COUNT:
            raise CommandError(
                "Release property bootstrap was incomplete: "
                f"expected {EXPECTED_PROPERTY_COUNT}, found {imported_count}."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Release property bootstrap complete: {imported_count} properties loaded."
            )
        )
