"""Hostaway property synchronization without network calls inside transactions."""

import logging
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.integrations.models import IntegrationSyncRun
from apps.properties.models import Amenity, Property, PropertyAmenity, PropertyImage

from .client import HostawayClient
from .exceptions import (
    HostawayAuthenticationError,
    HostawayError,
    HostawaySyncAlreadyRunningError,
)
from .listing_validators import (
    HostawayAmenityDefinition,
    HostawayListing,
    HostawayListingImage,
    normalize_amenity_definitions,
    normalize_listing,
)
from .locks import hostaway_property_sync_lock

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PropertySyncReport:
    fetched: int = 0
    properties_created: int = 0
    properties_updated: int = 0
    properties_failed: int = 0
    images_created: int = 0
    images_updated: int = 0
    images_deactivated: int = 0
    amenities_created: int = 0
    amenities_linked: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _UnitCounts:
    property_created: int = 0
    property_updated: int = 0
    images_created: int = 0
    images_updated: int = 0
    images_deactivated: int = 0
    amenities_created: int = 0
    amenities_linked: int = 0
    skipped: int = 0


def sync_properties(
    *,
    listing_id: int | None = None,
    include_images: bool = True,
    include_amenities: bool = True,
    dry_run: bool = False,
    force: bool = False,
    limit: int | None = None,
    client: HostawayClient | None = None,
    triggered_by_id: int | None = None,
) -> PropertySyncReport:
    """Synchronize listings into short, isolated database transactions."""
    if listing_id is not None and listing_id <= 0:
        raise ValueError("listing_id must be positive.")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive.")

    with hostaway_property_sync_lock():
        return _run_sync(
            listing_id=listing_id,
            include_images=include_images,
            include_amenities=include_amenities,
            dry_run=dry_run,
            force=force,
            limit=limit,
            client=client,
            triggered_by_id=triggered_by_id,
        )


def _run_sync(
    *,
    listing_id: int | None,
    include_images: bool,
    include_amenities: bool,
    dry_run: bool,
    force: bool,
    limit: int | None,
    client: HostawayClient | None,
    triggered_by_id: int | None,
) -> PropertySyncReport:
    report = PropertySyncReport()
    sync_run = _create_sync_run(
        dry_run=dry_run,
        triggered_by_id=triggered_by_id,
        metadata={
            "listing_id": listing_id,
            "include_images": include_images,
            "include_amenities": include_amenities,
            "force": force,
            "limit": limit,
        },
    )

    owns_client = client is None
    api_client: HostawayClient | None = None
    try:
        api_client = client or HostawayClient()
        raw_listings = _fetch_listings(
            api_client,
            report,
            listing_id=listing_id,
            include_resources=include_images or include_amenities,
            limit=limit,
        )
        amenity_definitions: dict[int, HostawayAmenityDefinition] = {}
        if include_amenities:
            try:
                raw_amenities = api_client.get_amenities()
                amenity_definitions, definition_errors = normalize_amenity_definitions(
                    raw_amenities
                )
                report.errors.extend(definition_errors)
                report.skipped += len(definition_errors)
            except HostawayAuthenticationError:
                raise
            except HostawayError as exc:
                report.errors.append(f"Amenity definitions unavailable: {exc}")
                logger.warning("Hostaway amenities could not be synchronized: %s", exc)
    except HostawayError:
        _finish_sync_run(sync_run, report, forced_status=IntegrationSyncRun.Status.FAILED)
        raise
    finally:
        if owns_client and api_client is not None:
            api_client.close()

    listings: list[HostawayListing] = []
    for index, payload in enumerate(raw_listings):
        try:
            listing = normalize_listing(payload)
        except HostawayError as exc:
            report.properties_failed += 1
            report.errors.append(f"Listing payload {index} rejected: {exc}")
            logger.warning("Rejected malformed Hostaway listing at index=%s.", index)
            continue
        listings.append(listing)
        report.errors.extend(
            f"Listing {listing.listing_id}: {error}" for error in listing.validation_errors
        )
        report.skipped += len(listing.validation_errors)

    dry_seen_amenities: set[int] = set()
    for listing in listings:
        try:
            if dry_run:
                counts = _simulate_unit(
                    listing,
                    amenity_definitions,
                    include_images=include_images,
                    include_amenities=include_amenities,
                    force=force,
                    dry_seen_amenities=dry_seen_amenities,
                )
            else:
                with transaction.atomic():
                    counts = _persist_unit(
                        listing,
                        amenity_definitions,
                        include_images=include_images,
                        include_amenities=include_amenities,
                        force=force,
                    )
        except (DatabaseError, ValidationError, ValueError, TypeError):
            report.properties_failed += 1
            report.errors.append(f"Listing {listing.listing_id}: database update failed")
            logger.exception(
                "Failed to persist Hostaway listing id=%s.",
                listing.listing_id,
            )
            continue
        _merge_counts(report, counts)

    _finish_sync_run(sync_run, report)
    return report


def _fetch_listings(
    client: HostawayClient,
    report: PropertySyncReport,
    *,
    listing_id: int | None,
    include_resources: bool,
    limit: int | None,
) -> list[dict[str, object]]:
    if listing_id is not None:
        record = client.get_listing(
            listing_id,
            include_resources=include_resources,
        )
        report.fetched = 1
        return [record]

    records: list[dict[str, object]] = []
    offset = 0
    while limit is None or len(records) < limit:
        remaining = 100 if limit is None else min(100, limit - len(records))
        page, count = client.get_listings(
            limit=remaining,
            offset=offset,
            include_resources=include_resources,
        )
        records.extend(page)
        report.fetched += len(page)
        offset += len(page)
        if not page:
            break
        if count is not None:
            if offset >= count:
                break
            continue
        if len(page) < remaining:
            break
    return records


def _persist_unit(
    listing: HostawayListing,
    amenity_definitions: dict[int, HostawayAmenityDefinition],
    *,
    include_images: bool,
    include_amenities: bool,
    force: bool,
) -> _UnitCounts:
    counts = _UnitCounts()
    existing = Property.objects.filter(hostaway_listing_id=listing.listing_id).first()
    if _is_stale(existing, listing, force):
        counts.skipped = 1
        return counts

    operational = _operational_defaults(listing)
    create_defaults = {
        **operational,
        "slug": _unique_slug(listing.name, listing.listing_id),
        "name_en": listing.name,
        "name_ar": "",
        "city_en": listing.city,
        "city_ar": "",
    }
    property_obj, created = Property.objects.update_or_create(
        hostaway_listing_id=listing.listing_id,
        defaults=operational,
        create_defaults=create_defaults,
    )
    if created:
        counts.property_created = 1
    else:
        counts.property_updated = 1

    if include_images and listing.images_present:
        image_counts = _sync_images(property_obj, listing.images)
        counts.images_created = image_counts[0]
        counts.images_updated = image_counts[1]
        counts.images_deactivated = image_counts[2]
    if include_amenities and listing.amenities_present:
        amenity_counts = _sync_amenities(
            property_obj,
            listing,
            amenity_definitions,
        )
        counts.amenities_created = amenity_counts[0]
        counts.amenities_linked = amenity_counts[1]
    return counts


def _simulate_unit(
    listing: HostawayListing,
    amenity_definitions: dict[int, HostawayAmenityDefinition],
    *,
    include_images: bool,
    include_amenities: bool,
    force: bool,
    dry_seen_amenities: set[int],
) -> _UnitCounts:
    counts = _UnitCounts()
    existing = Property.objects.filter(hostaway_listing_id=listing.listing_id).first()
    if _is_stale(existing, listing, force):
        counts.skipped = 1
        return counts
    if existing is None:
        counts.property_created = 1
    else:
        counts.property_updated = 1

    if include_images and listing.images_present:
        existing_images = (
            {
                image.sync_key: image
                for image in existing.images.filter(source=PropertyImage.Source.HOSTAWAY)
            }
            if existing
            else {}
        )
        incoming_keys = {image.sync_key for image in listing.images}
        counts.images_created = sum(
            image.sync_key not in existing_images for image in listing.images
        )
        counts.images_updated = len(listing.images) - counts.images_created
        counts.images_deactivated = sum(
            image.is_active_at_source and key not in incoming_keys
            for key, image in existing_images.items()
        )

    if include_amenities and listing.amenities_present:
        linked_ids = (
            set(
                existing.property_amenities.filter(
                    amenity__hostaway_amenity_id__in=[item.amenity_id for item in listing.amenities]
                ).values_list("amenity__hostaway_amenity_id", flat=True)
            )
            if existing
            else set()
        )
        for item in listing.amenities:
            if (
                item.amenity_id not in dry_seen_amenities
                and not Amenity.objects.filter(hostaway_amenity_id=item.amenity_id).exists()
            ):
                counts.amenities_created += 1
                dry_seen_amenities.add(item.amenity_id)
            if item.amenity_id not in linked_ids:
                counts.amenities_linked += 1
    return counts


def _operational_defaults(listing: HostawayListing) -> dict[str, object]:
    defaults: dict[str, object] = {
        "hostaway_name": listing.name,
        "hostaway_description": listing.description,
        "hostaway_internal_name": listing.internal_name,
        "hostaway_property_type_id": listing.hostaway_property_type_id,
        "room_type": listing.room_type,
        "person_capacity": listing.person_capacity,
        "bedrooms_number": listing.bedrooms_number,
        "beds_number": listing.beds_number,
        "bathrooms_number": listing.bathrooms_number,
        "address": listing.address,
        "public_address": listing.public_address,
        "city": listing.city,
        "state": listing.state,
        "country": listing.country,
        "country_code": listing.country_code,
        "zipcode": listing.zipcode,
        "latitude": listing.latitude,
        "longitude": listing.longitude,
        "currency_code": listing.currency_code,
        "average_review_rating": listing.average_review_rating,
        "hostaway_special_status": listing.special_status,
        "hostaway_is_active": listing.is_active,
        "last_synced_at": timezone.now(),
        "source_updated_at": listing.source_updated_at,
    }
    if listing.listing_map_id is not None:
        defaults["hostaway_listing_map_id"] = listing.listing_map_id
    return defaults


def _sync_images(
    property_obj: Property,
    images: tuple[HostawayListingImage, ...],
) -> tuple[int, int, int]:
    created_count = 0
    updated_count = 0
    active_pks: list[int] = []
    for image in images:
        image_obj = None
        if image.image_id is not None:
            image_obj = PropertyImage.objects.filter(
                property=property_obj, hostaway_image_id=image.image_id
            ).first()
        if image_obj is None:
            image_obj = PropertyImage.objects.filter(
                property=property_obj,
                hostaway_url=image.url,
                source=PropertyImage.Source.HOSTAWAY,
            ).first()
        if image_obj is None:
            image_obj = PropertyImage.objects.filter(
                property=property_obj,
                sync_key=image.sync_key,
                source=PropertyImage.Source.HOSTAWAY,
            ).first()
        if image_obj is None:
            image_obj = PropertyImage.objects.create(
                property=property_obj,
                hostaway_image_id=image.image_id,
                hostaway_url=image.url,
                hostaway_caption=image.caption,
                sync_key=image.sync_key,
                source=PropertyImage.Source.HOSTAWAY,
                sort_order=image.sort_order,
                hostaway_sort_order=image.sort_order,
                is_active_at_source=True,
                source_updated_at=image.source_updated_at,
            )
            created_count += 1
        else:
            image_obj.hostaway_image_id = image.image_id
            image_obj.hostaway_url = image.url
            image_obj.hostaway_caption = image.caption
            image_obj.sync_key = image.sync_key
            image_obj.hostaway_sort_order = image.sort_order
            image_obj.is_active_at_source = True
            image_obj.source_updated_at = image.source_updated_at
            image_obj.save(
                update_fields=[
                    "hostaway_image_id",
                    "hostaway_url",
                    "hostaway_caption",
                    "sync_key",
                    "hostaway_sort_order",
                    "is_active_at_source",
                    "source_updated_at",
                    "updated_at",
                ]
            )
            updated_count += 1
        active_pks.append(image_obj.pk)

    stale_images = property_obj.images.filter(
        source=PropertyImage.Source.HOSTAWAY,
        is_active_at_source=True,
    ).exclude(pk__in=active_pks)
    deactivated_count = stale_images.update(is_active_at_source=False)
    return created_count, updated_count, deactivated_count


def _sync_amenities(
    property_obj: Property,
    listing: HostawayListing,
    definitions: dict[int, HostawayAmenityDefinition],
) -> tuple[int, int]:
    created_count = 0
    linked_count = 0
    active_link_pks: list[int] = []
    for item in listing.amenities:
        definition = definitions.get(item.amenity_id)
        amenity_defaults: dict[str, object] = {"is_active": True}
        if definition is not None:
            amenity_defaults["name"] = definition.name
        amenity, amenity_created = Amenity.objects.update_or_create(
            hostaway_amenity_id=item.amenity_id,
            defaults=amenity_defaults,
            create_defaults={
                "name": definition.name if definition else "",
                "is_active": True,
            },
        )
        created_count += int(amenity_created)
        link, link_created = PropertyAmenity.objects.get_or_create(
            property=property_obj,
            amenity=amenity,
            defaults={
                "source": PropertyAmenity.Source.HOSTAWAY,
                "sort_order": item.sort_order,
                "is_active_at_source": True,
            },
        )
        if not link_created and not link.is_active_at_source:
            link.is_active_at_source = True
            link.save(update_fields=["is_active_at_source"])
        linked_count += int(link_created)
        active_link_pks.append(link.pk)

    property_obj.property_amenities.filter(
        source=PropertyAmenity.Source.HOSTAWAY,
        is_active_at_source=True,
    ).exclude(pk__in=active_link_pks).update(is_active_at_source=False)
    return created_count, linked_count


def _unique_slug(name: str, listing_id: int) -> str:
    base = slugify(name, allow_unicode=True)[:150].strip("-")
    if not base:
        base = f"unit-{listing_id}"
    candidate = base
    suffix = 2
    while Property.objects.filter(slug=candidate).exists():
        candidate = f"{base[:165]}-{suffix}"
        suffix += 1
    return candidate


def _is_stale(
    existing: Property | None,
    listing: HostawayListing,
    force: bool,
) -> bool:
    return bool(
        not force
        and existing
        and existing.source_updated_at
        and listing.source_updated_at
        and listing.source_updated_at < existing.source_updated_at
    )


def _merge_counts(report: PropertySyncReport, counts: _UnitCounts) -> None:
    report.properties_created += counts.property_created
    report.properties_updated += counts.property_updated
    report.images_created += counts.images_created
    report.images_updated += counts.images_updated
    report.images_deactivated += counts.images_deactivated
    report.amenities_created += counts.amenities_created
    report.amenities_linked += counts.amenities_linked
    report.skipped += counts.skipped


def _create_sync_run(
    *,
    dry_run: bool,
    triggered_by_id: int | None,
    metadata: dict[str, object],
) -> IntegrationSyncRun | None:
    if dry_run:
        return None
    try:
        with transaction.atomic():
            return IntegrationSyncRun.objects.create(
                sync_type=IntegrationSyncRun.SyncType.HOSTAWAY_PROPERTIES,
                status=IntegrationSyncRun.Status.RUNNING,
                started_at=timezone.now(),
                triggered_by_id=triggered_by_id,
                dry_run=False,
                metadata=metadata,
            )
    except IntegrityError as exc:
        raise HostawaySyncAlreadyRunningError(
            "Another Hostaway property sync is already running."
        ) from exc


def _finish_sync_run(
    sync_run: IntegrationSyncRun | None,
    report: PropertySyncReport,
    *,
    forced_status: str | None = None,
) -> None:
    if sync_run is None:
        return
    successful = report.properties_created + report.properties_updated
    if forced_status is not None:
        status = forced_status
    elif report.properties_failed and not successful:
        status = IntegrationSyncRun.Status.FAILED
    elif report.properties_failed or report.errors:
        status = IntegrationSyncRun.Status.PARTIALLY_SUCCEEDED
    else:
        status = IntegrationSyncRun.Status.SUCCEEDED
    sync_run.status = status
    sync_run.completed_at = timezone.now()
    sync_run.fetched_count = report.fetched
    sync_run.created_count = report.properties_created
    sync_run.updated_count = report.properties_updated
    sync_run.skipped_count = report.skipped
    sync_run.failed_count = report.properties_failed
    sync_run.error_summary = "\n".join(report.errors[:20])[:4000]
    sync_run.metadata = {
        **sync_run.metadata,
        "images_created": report.images_created,
        "images_updated": report.images_updated,
        "images_deactivated": report.images_deactivated,
        "amenities_created": report.amenities_created,
        "amenities_linked": report.amenities_linked,
    }
    sync_run.save(
        update_fields=[
            "status",
            "completed_at",
            "fetched_count",
            "created_count",
            "updated_count",
            "skipped_count",
            "failed_count",
            "error_summary",
            "metadata",
        ]
    )
