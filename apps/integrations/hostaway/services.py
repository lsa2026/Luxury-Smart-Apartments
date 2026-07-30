"""Application services for syncing Hostaway reviews."""

import logging
from dataclasses import dataclass, field
from typing import Any

from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.properties.models import Property
from apps.reviews.models import Review

from .client import HostawayClient
from .exceptions import HostawayResponseError
from .validators import HostawayReview, normalize_review

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncReport:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    match_strategies: dict[str, int] = field(
        default_factory=lambda: {
            "listing_map_id": 0,
            "listing_id_fallback": 0,
            "unmatched": 0,
        }
    )


def sync_reviews(
    *,
    listing_id: int | None = None,
    departure_from: str | None = None,
    departure_to: str | None = None,
    dry_run: bool = False,
    client: HostawayClient | None = None,
    page_size: int = 100,
) -> SyncReport:
    """Fetch all matching pages, then persist each review in a short transaction."""
    report = SyncReport()
    raw_reviews = _fetch_all_pages(
        report=report,
        listing_id=listing_id,
        departure_from=departure_from,
        departure_to=departure_to,
        client=client,
        page_size=page_size,
    )

    normalized: list[HostawayReview] = []
    for raw_review in raw_reviews:
        try:
            review = normalize_review(raw_review)
        except HostawayResponseError:
            report.failed += 1
            logger.warning("Skipped one malformed Hostaway review payload.")
            continue
        if not _is_eligible(review):
            report.skipped += 1
            continue
        normalized.append(review)

    listing_ids = {review.hostaway_listing_map_id for review in normalized}
    properties_by_map_id = {
        item.hostaway_listing_map_id: item
        for item in Property.objects.filter(hostaway_listing_map_id__in=listing_ids)
    }
    fallback_ids = listing_ids.difference(properties_by_map_id)
    properties_by_listing_id = {
        item.hostaway_listing_id: item
        for item in Property.objects.filter(hostaway_listing_id__in=fallback_ids)
    }

    for review in normalized:
        property_obj = properties_by_map_id.get(review.hostaway_listing_map_id)
        if property_obj is not None:
            match_strategy = "listing_map_id"
        else:
            property_obj = properties_by_listing_id.get(review.hostaway_listing_map_id)
            match_strategy = "listing_id_fallback" if property_obj is not None else "unmatched"
        report.match_strategies[match_strategy] += 1
        defaults = _review_defaults(review, property_obj)
        existing = Review.objects.filter(hostaway_review_id=review.hostaway_review_id).exists()
        if dry_run:
            if existing:
                report.updated += 1
            else:
                report.created += 1
            continue
        try:
            with transaction.atomic():
                _, created = Review.objects.update_or_create(
                    hostaway_review_id=review.hostaway_review_id,
                    defaults=defaults,
                )
        except (DatabaseError, ValueError, TypeError):
            report.failed += 1
            logger.exception("Failed to persist one Hostaway review.")
            continue
        if created:
            report.created += 1
        else:
            report.updated += 1
    return report


def _fetch_all_pages(
    *,
    report: SyncReport,
    listing_id: int | None,
    departure_from: str | None,
    departure_to: str | None,
    client: HostawayClient | None,
    page_size: int,
) -> list[dict[str, Any]]:
    owns_client = client is None
    api_client = client or HostawayClient()
    records: list[dict[str, Any]] = []
    offset = 0
    try:
        while True:
            page, total = api_client.get_reviews(
                listing_map_ids=[listing_id] if listing_id is not None else None,
                limit=page_size,
                offset=offset,
                sort_by="departureDate",
                sort_order="desc",
                review_type=Review.Type.GUEST_TO_HOST,
                statuses=[Review.Status.PUBLISHED],
                departure_date_start=departure_from,
                departure_date_end=departure_to,
            )
            records.extend(page)
            report.fetched += len(page)
            offset += len(page)
            if not page or len(page) < page_size:
                break
            if total is not None and offset >= total:
                break
    finally:
        if owns_client:
            api_client.close()
    return records


def _is_eligible(review: HostawayReview) -> bool:
    return bool(
        review.review_type == Review.Type.GUEST_TO_HOST
        and review.status == Review.Status.PUBLISHED
        and review.rating is not None
        and review.public_review
    )


def _review_defaults(
    review: HostawayReview,
    property_obj: Property | None,
) -> dict[str, object]:
    return {
        "property": property_obj,
        "hostaway_listing_map_id": review.hostaway_listing_map_id,
        "hostaway_reservation_id": review.hostaway_reservation_id,
        "external_review_id": review.external_review_id,
        "channel_id": review.channel_id,
        "review_type": review.review_type,
        "status": review.status,
        "guest_name": review.guest_name,
        "rating": review.rating,
        "public_review": review.public_review,
        "reviewee_response": review.reviewee_response,
        "arrival_date": review.arrival_date,
        "departure_date": review.departure_date,
        "source_updated_at": review.source_updated_at,
        "synced_at": timezone.now(),
    }
