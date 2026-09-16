"""Apply curated guest-facing property copy without touching Hostaway source fields."""

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.properties.models import Property

CONTENT_PATH = Path(__file__).resolve().parents[2] / "data" / "property_localizations.json"
LOCALIZED_FIELDS = (
    "name_ar",
    "name_en",
    "name_fr",
    "short_description_ar",
    "short_description_en",
    "short_description_fr",
    "description_ar",
    "description_en",
    "description_fr",
    "city_ar",
    "city_en",
    "city_fr",
    "seo_title_ar",
    "seo_title_en",
    "seo_title_fr",
    "seo_description_ar",
    "seo_description_en",
    "seo_description_fr",
)
PUBLIC_VISIBILITY_FIELD = "public_visibility"


class Command(BaseCommand):
    help = "Apply the reviewed Arabic, English and French property content."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: Any) -> None:
        content = _load_content()
        listing_ids = [int(listing_id) for listing_id in content]
        properties = {
            property_obj.hostaway_listing_id: property_obj
            for property_obj in Property.objects.filter(hostaway_listing_id__in=listing_ids)
        }
        missing = sorted(set(listing_ids) - set(properties))
        if missing:
            raise CommandError(f"Missing local properties for Hostaway listing ids: {missing}")

        updated = 0
        for listing_id, values in content.items():
            property_obj = properties[int(listing_id)]
            changed_fields = [
                field_name
                for field_name in LOCALIZED_FIELDS
                if getattr(property_obj, field_name) != values[field_name]
            ]
            if (
                PUBLIC_VISIBILITY_FIELD in values
                and property_obj.is_visible != values[PUBLIC_VISIBILITY_FIELD]
            ):
                changed_fields.append("is_visible")
            if not changed_fields:
                continue
            updated += 1
            if not options["dry_run"]:
                for field_name in changed_fields:
                    value_key = (
                        PUBLIC_VISIBILITY_FIELD
                        if field_name == "is_visible"
                        else field_name
                    )
                    setattr(property_obj, field_name, values[value_key])
                property_obj.save(update_fields=[*changed_fields, "updated_at"])

        suffix = " (dry run)" if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Property localizations applied{suffix}: {updated} updated, "
                f"{len(listing_ids) - updated} already current."
            )
        )


def _load_content() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(CONTENT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Could not read curated property content: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise CommandError("Curated property content must be a non-empty object.")

    for listing_id, values in payload.items():
        if (
            not isinstance(listing_id, str)
            or not listing_id.isdigit()
            or not isinstance(values, dict)
        ):
            raise CommandError("Curated property content has an invalid listing entry.")
        missing_fields = [
            field_name for field_name in LOCALIZED_FIELDS if not values.get(field_name)
        ]
        if missing_fields:
            raise CommandError(
                f"Curated property content for listing {listing_id} is missing: {missing_fields}"
            )
        if any(not isinstance(values[field_name], str) for field_name in LOCALIZED_FIELDS):
            raise CommandError(f"Curated property content for listing {listing_id} must be text.")
        if (
            PUBLIC_VISIBILITY_FIELD in values
            and not isinstance(values[PUBLIC_VISIBILITY_FIELD], bool)
        ):
            raise CommandError(
                "Curated property content for listing "
                f"{listing_id} has an invalid public visibility flag."
            )
    return payload
