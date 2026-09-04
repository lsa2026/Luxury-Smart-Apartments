"""Automatic publication policy for Hostaway listings."""

from dataclasses import dataclass

from django.conf import settings

from apps.properties.models import Property


@dataclass(frozen=True, slots=True)
class PublishReadiness:
    is_ready: bool
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]


def evaluate_listing_publish_readiness(property_obj: Property) -> PublishReadiness:
    """Evaluate source completeness without changing local editorial choices."""
    blockers: list[str] = []
    warnings: list[str] = []

    if property_obj.hostaway_listing_id <= 0:
        blockers.append("invalid_listing_id")
    if (
        settings.HOSTAWAY_LIVE_BOOKING_ENABLED
        and property_obj.hostaway_listing_map_id is None
    ):
        blockers.append("missing_listing_map_id")
    if settings.HOSTAWAY_AUTO_PUBLISH_REQUIRE_ACTIVE and (
        not property_obj.hostaway_is_active
        or property_obj.hostaway_special_status.strip().casefold() == "archived"
    ):
        blockers.append("inactive_or_archived")
    if not (property_obj.name_ar or property_obj.name_en or property_obj.hostaway_name):
        blockers.append("missing_name")
    if settings.HOSTAWAY_AUTO_PUBLISH_REQUIRE_IMAGE and not property_obj.images.public().exists():
        blockers.append("missing_visible_image")
    if settings.HOSTAWAY_AUTO_PUBLISH_REQUIRE_CAPACITY and (
        property_obj.person_capacity is None or property_obj.person_capacity <= 0
    ):
        blockers.append("missing_capacity")
    if settings.HOSTAWAY_AUTO_PUBLISH_REQUIRE_CURRENCY and (
        len(property_obj.currency_code) != 3 or not property_obj.currency_code.isalpha()
    ):
        blockers.append("missing_currency")
    if settings.HOSTAWAY_AUTO_PUBLISH_REQUIRE_CITY and not property_obj.display_city:
        blockers.append("missing_city")
    elif not property_obj.display_city:
        warnings.append("missing_city")
    if property_obj.source_missing:
        blockers.append("missing_from_source")

    return PublishReadiness(
        is_ready=not blockers,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )
