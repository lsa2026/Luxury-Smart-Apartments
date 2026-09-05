"""Fill Arabic and French amenity names from the hand-written map.

Amenity names arrive from Hostaway in English and the site is Arabic, so a guest
reads "Washing Machine" on an otherwise Arabic page. The names live in the
database and the sync never overwrites them, so seeding them once is enough.

An amenity that already carries an Arabic name is never touched: the
administration's wording outranks anything shipped in the repository. An amenity
missing from the map keeps its English name and is reported, so a gap is visible
rather than silently guessed at.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from apps.properties.amenity_translations import copy_for
from apps.properties.models import Amenity


class Command(BaseCommand):
    help = "Seed Arabic and French amenity names, categories and icons."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Also seed amenities currently marked inactive.",
        )

    def handle(self, *args: object, **options: Any) -> None:
        amenities = Amenity.objects.all().order_by("id")
        if not options["include_inactive"]:
            amenities = amenities.filter(is_active=True)

        seeded = skipped_existing = unmapped = 0
        missing: list[str] = []

        for amenity in amenities:
            source = amenity.name or amenity.name_en
            entry = copy_for(source)
            if entry is None:
                unmapped += 1
                missing.append(source or f"#{amenity.pk}")
                continue

            updates: dict[str, str] = {}
            # The administration's own wording is never replaced.
            if not amenity.name_ar.strip():
                updates["name_ar"] = entry.name_ar
            if not amenity.name_fr.strip():
                updates["name_fr"] = entry.name_fr
            if not amenity.name_en.strip() and source:
                updates["name_en"] = source
            if not amenity.category.strip():
                updates["category"] = entry.category
            if not amenity.icon_key.strip():
                updates["icon_key"] = entry.icon_key

            if not updates:
                skipped_existing += 1
                continue
            seeded += 1
            if options["dry_run"]:
                continue
            with transaction.atomic():
                Amenity.objects.filter(pk=amenity.pk).update(**updates)

        mode = " (dry run)" if options["dry_run"] else ""
        self.stdout.write(
            f"Amenity names{mode}: seeded={seeded}, already_named={skipped_existing}, "
            f"unmapped={unmapped}"
        )
        if missing:
            self.stdout.write("Not in the map, left in English:")
            for name in sorted(set(missing)):
                self.stdout.write(f"  - {name}")
