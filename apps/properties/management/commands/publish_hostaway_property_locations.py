"""Publish verified property locations and their Google Maps business profiles."""

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.properties.models import Property

BUSINESS_PROFILE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "google_maps_business_profiles.json"
)


class Command(BaseCommand):
    help = "Publish property locations, respecting individually reviewed public-map corrections."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: Any) -> None:
        business_profiles = _load_business_profiles()
        properties = list(Property.objects.order_by("hostaway_listing_id"))
        if not properties:
            raise CommandError("There are no local properties to publish.")

        incomplete = [
            str(property_obj.hostaway_listing_id)
            for property_obj in properties
            if (
                not property_obj.public_address.strip()
                or property_obj.latitude is None
                or property_obj.longitude is None
            )
        ]
        if incomplete:
            raise CommandError(
                "Cannot publish locations with missing public address or coordinates: "
                + ", ".join(incomplete)
            )

        listing_ids = {str(property_obj.hostaway_listing_id) for property_obj in properties}
        mapped_listing_ids = set(business_profiles)
        if listing_ids != mapped_listing_ids:
            missing = sorted(listing_ids - mapped_listing_ids)
            extra = sorted(mapped_listing_ids - listing_ids)
            details = []
            if missing:
                details.append(f"missing mappings: {', '.join(missing)}")
            if extra:
                details.append(f"unexpected mappings: {', '.join(extra)}")
            message = "Google Maps business profile mapping is incomplete ("
            raise CommandError(message + "; ".join(details) + ").")

        updated = 0
        for property_obj in properties:
            listing_key = str(property_obj.hostaway_listing_id)
            profile = business_profiles[listing_key]
            google_maps_cid = profile["google_maps_cid"]
            # A reviewed correction changes only the website map, never the
            # imported Hostaway coordinates or any booking information.
            latitude = (
                Decimal(profile["public_location_latitude"])
                if "public_location_latitude" in profile
                else property_obj.latitude
            )
            longitude = (
                Decimal(profile["public_location_longitude"])
                if "public_location_longitude" in profile
                else property_obj.longitude
            )
            changed_fields: list[str] = []
            if not property_obj.public_location_enabled:
                property_obj.public_location_enabled = True
                changed_fields.append("public_location_enabled")
            if property_obj.public_location_latitude != latitude:
                property_obj.public_location_latitude = latitude
                changed_fields.append("public_location_latitude")
            if property_obj.public_location_longitude != longitude:
                property_obj.public_location_longitude = longitude
                changed_fields.append("public_location_longitude")
            if property_obj.google_maps_cid != google_maps_cid:
                property_obj.google_maps_cid = google_maps_cid
                changed_fields.append("google_maps_cid")
            if not changed_fields:
                continue
            updated += 1
            if not options["dry_run"]:
                property_obj.save(update_fields=[*changed_fields, "updated_at"])

        suffix = " (dry run)" if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Property locations published{suffix}: {updated} updated, "
                f"{len(properties) - updated} already current."
            )
        )


def _load_business_profiles() -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(BUSINESS_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Could not read Google Maps business profile mapping: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise CommandError("Google Maps business profile mapping must be a non-empty object.")
    for listing_id, values in payload.items():
        if (
            not isinstance(listing_id, str)
            or not listing_id.isdigit()
            or not isinstance(values, dict)
            or not isinstance(values.get("google_maps_cid"), str)
            or not values["google_maps_cid"].isdigit()
        ):
            raise CommandError("Google Maps business profile mapping contains an invalid entry.")
        coordinate_fields = ("public_location_latitude", "public_location_longitude")
        coordinates = [values.get(field) for field in coordinate_fields]
        if any(field in values for field in coordinate_fields):
            try:
                if not all(isinstance(value, str) for value in coordinates):
                    raise ValueError
                latitude, longitude = (Decimal(value) for value in coordinates)
                if (
                    not latitude.is_finite()
                    or not longitude.is_finite()
                    or not -90 <= latitude <= 90
                    or not -180 <= longitude <= 180
                    or latitude.as_tuple().exponent < -6
                    or longitude.as_tuple().exponent < -6
                ):
                    raise ValueError
            except (ValueError, InvalidOperation) as exc:
                raise CommandError(
                    "Public-map correction must be a valid coordinate pair."
                ) from exc
    return payload
