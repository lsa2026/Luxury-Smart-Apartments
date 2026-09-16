"""Assign approved Trustindex widgets to their local properties."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.properties.models import Property


class Command(BaseCommand):
    help = "Apply the reviewed Trustindex widget mapping to local properties."

    def handle(self, *args: object, **options: object) -> None:
        path = Path(__file__).resolve().parents[2] / "data" / "trustindex_widgets.json"
        try:
            mapping = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError("Trustindex widget mapping could not be read.") from exc
        changed = 0
        for raw_listing_id, values in mapping.items():
            widget_id = str(values.get("widget_id", "")).strip()
            if len(widget_id) < 20 or not widget_id.isalnum():
                raise CommandError(f"Invalid Trustindex widget ID for listing {raw_listing_id}.")
            try:
                property_obj = Property.objects.get(hostaway_listing_id=int(raw_listing_id))
            except Property.DoesNotExist as exc:
                raise CommandError(
                    f"No local property found for listing {raw_listing_id}."
                ) from exc
            if property_obj.trustindex_widget_id != widget_id:
                property_obj.trustindex_widget_id = widget_id
                property_obj.save(update_fields=["trustindex_widget_id"])
                changed += 1
        self.stdout.write(
            self.style.SUCCESS(f"Assigned Trustindex widgets for {changed} properties.")
        )
