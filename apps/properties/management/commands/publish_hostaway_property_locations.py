"""Publish verified property locations and their Google Maps business profiles."""

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.properties.models import Property

BUSINESS_PROFILE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "google_maps_business_profiles.json"
)


class Command(BaseCommand):
    help = "Publish exact listing coordinates with their verified Google Maps profiles."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: Any) -> None:
        mapping = _load_mapping()
        properties = list(Property.objects.order_by("hostaway_listing_id"))
        if not properties:
            raise CommandError("There are no local properties to publish.")
        updated = 0
        for item in properties:
            values = mapping.get(str(item.hostaway_listing_id))
            if not values or item.latitude is None or item.longitude is None:
                continue
            cid = values["google_maps_cid"]
            changes: list[str] = []
            for field, value in (
                ("public_location_enabled", True),
                ("public_location_latitude", item.latitude),
                ("public_location_longitude", item.longitude),
                ("google_maps_cid", cid),
            ):
                if getattr(item, field) != value:
                    setattr(item, field, value)
                    changes.append(field)
            if changes:
                updated += 1
                if not options["dry_run"]:
                    item.save(update_fields=[*changes, "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"Published {updated} property locations."))


def _load_mapping() -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(BUSINESS_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError("Google Maps profile mapping could not be read.") from exc
    if not isinstance(payload, dict) or not payload:
        raise CommandError("Google Maps profile mapping is invalid.")
    for listing_id, values in payload.items():
        cid = values.get("google_maps_cid") if isinstance(values, dict) else None
        if (
            not isinstance(listing_id, str)
            or not listing_id.isdigit()
            or not isinstance(cid, str)
            or not cid.isdigit()
        ):
            raise CommandError("Google Maps profile mapping contains an invalid entry.")
    return payload
