"""Read-only Hostaway contract verification with privacy-safe reporting."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Any
from urllib.parse import parse_qsl, urlparse

from .client import HostawayClient
from .exceptions import HostawayResponseError
from .listing_validators import (
    HostawayCollectionPage,
    HostawayObjectDocument,
    normalize_listing,
)

ROOT_FIELDS = ("status", "result", "limit", "offset", "count", "page", "totalPages")
LISTING_FIELDS = (
    "id",
    "listingMapId",
    "name",
    "internalListingName",
    "description",
    "propertyTypeId",
    "roomType",
    "personCapacity",
    "bedroomsNumber",
    "bedsNumber",
    "bathroomsNumber",
    "guestBathroomsNumber",
    "address",
    "publicAddress",
    "city",
    "state",
    "country",
    "countryCode",
    "zipcode",
    "lat",
    "lng",
    "currencyCode",
    "averageReviewRating",
    "specialStatus",
    "listingImages",
    "listingAmenities",
    "updatedOn",
    "latestActivityOn",
)
STRING_FIELDS = {
    "name",
    "internalListingName",
    "description",
    "roomType",
    "address",
    "publicAddress",
    "city",
    "state",
    "country",
    "countryCode",
    "zipcode",
    "currencyCode",
    "specialStatus",
    "updatedOn",
    "latestActivityOn",
}
INTEGER_FIELDS = {
    "id",
    "listingMapId",
    "propertyTypeId",
    "personCapacity",
    "bedroomsNumber",
    "bedsNumber",
}
DECIMAL_FIELDS = {
    "bathroomsNumber",
    "guestBathroomsNumber",
    "lat",
    "lng",
    "averageReviewRating",
}
ARRAY_FIELDS = {"listingImages", "listingAmenities"}
SENSITIVE_QUERY_PARTS = ("signature", "token", "credential", "expires", "x-amz-")
PRIVATE_REVIEW_FIELDS = {
    "privateFeedback",
    "guestName",
    "reservationId",
    "externalReviewId",
    "guestEmail",
    "guestPhone",
    "email",
    "phone",
}


@dataclass(slots=True)
class ListingVerification:
    identifier: int | None
    present_fields: list[str]
    missing_fields: list[str]
    unknown_fields: list[str]
    field_types: dict[str, str]
    type_mismatches: list[str]
    validation_errors: list[str]
    special_status: str
    image_count: int = 0
    image_domains: list[str] = field(default_factory=list)
    images_missing_id: int = 0
    images_missing_url: int = 0
    non_https_images: int = 0
    duplicate_images: int = 0
    temporary_image_urls: int = 0
    image_fields: dict[str, str] = field(default_factory=dict)
    amenity_count: int = 0
    amenity_fields: dict[str, str] = field(default_factory=dict)

    @property
    def strict_errors(self) -> list[str]:
        errors: list[str] = []
        if self.identifier is None:
            errors.append("primary listing identifier is missing or invalid")
        if "name" in self.type_mismatches:
            errors.append("basic field name has an incompatible type")
        return errors


@dataclass(slots=True)
class ReviewVerification:
    fetched: int = 0
    field_types: dict[str, str] = field(default_factory=dict)
    matched_listing_ids: int = 0
    unmatched_listing_ids: int = 0
    rating_min: Decimal | None = None
    rating_max: Decimal | None = None


@dataclass(slots=True)
class VerificationReport:
    connection_ok: bool
    authentication_seconds: float
    masked_account_id: str
    root_present_fields: list[str]
    root_missing_fields: list[str]
    root_unknown_fields: list[str]
    root_field_types: dict[str, str]
    listings: list[ListingVerification]
    amenities_checked: bool = False
    amenity_definition_count: int = 0
    amenity_definition_fields: dict[str, str] = field(default_factory=dict)
    reviews: ReviewVerification | None = None
    recommendations: list[str] = field(default_factory=list)

    @property
    def strict_errors(self) -> list[str]:
        return [error for listing in self.listings for error in listing.strict_errors]


def verify_hostaway(
    *,
    client: HostawayClient,
    listing_limit: int = 3,
    listing_id: int | None = None,
    include_reviews: bool = False,
    include_amenities: bool = False,
) -> VerificationReport:
    """Inspect remote schemas without importing or writing Django models."""
    started = perf_counter()
    client.authenticate()
    authentication_seconds = perf_counter() - started

    if listing_id is None:
        page = client.get_listings_page(
            limit=listing_limit,
            offset=0,
            include_resources=True,
        )
        records = list(page.records)
        root = _root_schema(page)
    else:
        document = client.get_listing_document(listing_id, include_resources=True)
        records = [document.record]
        root = _object_root_schema(document)

    listing_checks = [_inspect_listing(record) for record in records]
    report = VerificationReport(
        connection_ok=True,
        authentication_seconds=authentication_seconds,
        masked_account_id=client.masked_account_id,
        root_present_fields=root[0],
        root_missing_fields=root[1],
        root_unknown_fields=root[2],
        root_field_types=root[3],
        listings=listing_checks,
    )

    if include_amenities:
        amenity_page = client.get_amenities_page()
        report.amenities_checked = True
        report.amenity_definition_count = len(amenity_page.records)
        report.amenity_definition_fields = _aggregate_schema(amenity_page.records)

    listing_ids = {item.identifier for item in listing_checks if item.identifier is not None}
    if include_reviews and listing_ids:
        report.reviews = _inspect_reviews(client, listing_ids)

    report.recommendations = _recommendations(report)
    return report


def _root_schema(
    page: HostawayCollectionPage,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    present = sorted(set(ROOT_FIELDS).intersection(page.fields))
    missing = sorted(set(ROOT_FIELDS).difference(page.fields))
    unknown = sorted(page.fields.difference(ROOT_FIELDS))
    field_types = {
        "status": _type_name(page.status),
        "result": "array",
        "limit": _type_name(page.limit),
        "offset": _type_name(page.offset),
        "count": _type_name(page.count),
        "page": _type_name(page.page),
        "totalPages": _type_name(page.total_pages),
    }
    return present, missing, unknown, field_types


def _object_root_schema(
    document: HostawayObjectDocument,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    present = sorted(set(ROOT_FIELDS).intersection(document.fields))
    missing = sorted(set(ROOT_FIELDS).difference(document.fields))
    unknown = sorted(document.fields.difference(ROOT_FIELDS))
    field_types = {
        name: (
            _type_name(document.status)
            if name == "status"
            else "object"
            if name == "result"
            else "not-present"
        )
        for name in ROOT_FIELDS
    }
    return present, missing, unknown, field_types


def _inspect_listing(payload: dict[str, Any]) -> ListingVerification:
    present = sorted(set(LISTING_FIELDS).intersection(payload))
    missing = sorted(set(LISTING_FIELDS).difference(payload))
    unknown = sorted(set(payload).difference(LISTING_FIELDS))
    field_types = {name: _type_name(value) for name, value in sorted(payload.items())}
    mismatches = [name for name in present if not _is_compatible_listing_type(name, payload[name])]
    validation_errors: list[str] = []
    identifier: int | None = None
    special_status = (
        payload.get("specialStatus") if isinstance(payload.get("specialStatus"), str) else ""
    )
    try:
        listing = normalize_listing(payload)
        identifier = listing.listing_id
        validation_errors.extend(listing.validation_errors)
        special_status = listing.special_status
    except HostawayResponseError as exc:
        validation_errors.append(str(exc))

    images = payload.get("listingImages")
    image_summary = _inspect_images(images if isinstance(images, list) else [])
    amenities = payload.get("listingAmenities")
    amenity_records = amenities if isinstance(amenities, list) else []

    return ListingVerification(
        identifier=identifier,
        present_fields=present,
        missing_fields=missing,
        unknown_fields=unknown,
        field_types=field_types,
        type_mismatches=mismatches,
        validation_errors=validation_errors,
        special_status=special_status,
        image_count=image_summary["count"],
        image_domains=image_summary["domains"],
        images_missing_id=image_summary["missing_id"],
        images_missing_url=image_summary["missing_url"],
        non_https_images=image_summary["non_https"],
        duplicate_images=image_summary["duplicates"],
        temporary_image_urls=image_summary["temporary"],
        image_fields=(
            _aggregate_schema(image for image in images if isinstance(image, dict))
            if isinstance(images, list)
            else {}
        ),
        amenity_count=len(amenity_records),
        amenity_fields=_aggregate_schema(
            amenity for amenity in amenity_records if isinstance(amenity, dict)
        ),
    )


def _inspect_images(images: list[Any]) -> dict[str, Any]:
    domains: set[str] = set()
    seen_ids: set[int] = set()
    seen_urls: set[str] = set()
    missing_id = missing_url = non_https = duplicates = temporary = 0
    valid_count = 0
    for image in images:
        if not isinstance(image, dict):
            continue
        valid_count += 1
        image_id = image.get("id")
        url = image.get("url")
        if not isinstance(image_id, int) or isinstance(image_id, bool):
            missing_id += 1
        elif image_id in seen_ids:
            duplicates += 1
        else:
            seen_ids.add(image_id)
        if not isinstance(url, str) or not url:
            missing_url += 1
            continue
        parsed = urlparse(url)
        if parsed.scheme.casefold() != "https":
            non_https += 1
        if parsed.hostname:
            domains.add(parsed.hostname.casefold())
        if url in seen_urls:
            duplicates += 1
        else:
            seen_urls.add(url)
        query_names = {name.casefold() for name, _ in parse_qsl(parsed.query)}
        if any(part in query_name for query_name in query_names for part in SENSITIVE_QUERY_PARTS):
            temporary += 1
    return {
        "count": valid_count,
        "domains": sorted(domains),
        "missing_id": missing_id,
        "missing_url": missing_url,
        "non_https": non_https,
        "duplicates": duplicates,
        "temporary": temporary,
    }


def _inspect_reviews(
    client: HostawayClient,
    listing_ids: set[int],
) -> ReviewVerification:
    records, _ = client.get_reviews(
        listing_map_ids=sorted(listing_ids),
        limit=100,
        offset=0,
        review_type="guest-to-host",
        statuses=["published"],
    )
    ratings: list[Decimal] = []
    matched = unmatched = 0
    for record in records:
        raw_listing_id = record.get("listingMapId")
        if _integer_value(raw_listing_id) in listing_ids:
            matched += 1
        else:
            unmatched += 1
        rating = _decimal_value(record.get("rating"))
        if rating is not None:
            ratings.append(rating)
    return ReviewVerification(
        fetched=len(records),
        field_types=_aggregate_schema(
            {name: value for name, value in record.items() if name not in PRIVATE_REVIEW_FIELDS}
            for record in records
        ),
        matched_listing_ids=matched,
        unmatched_listing_ids=unmatched,
        rating_min=min(ratings, default=None),
        rating_max=max(ratings, default=None),
    )


def _recommendations(report: VerificationReport) -> list[str]:
    recommendations: list[str] = []
    if report.root_missing_fields:
        if {"page", "totalPages"}.issubset(report.root_missing_fields):
            recommendations.append(
                "Hostaway omitted page/totalPages; retain offset pagination using limit/count."
            )
        else:
            recommendations.append("Review missing standard response metadata before live sync.")
    if any(item.type_mismatches for item in report.listings):
        recommendations.append("Align DTO field types listed as incompatible before live sync.")
    if any(item.images_missing_id for item in report.listings):
        recommendations.append("Keep the verified URL-hash fallback for images without IDs.")
    if any(item.temporary_image_urls for item in report.listings):
        recommendations.append("Do not persist signed query parameters longer than required.")
    unknown_statuses = {
        item.special_status
        for item in report.listings
        if item.special_status and item.special_status.strip().casefold() != "archived"
    }
    if unknown_statuses:
        recommendations.append(
            "Review unknown specialStatus values; they remain active unless archived."
        )
    if not recommendations:
        recommendations.append("No blocking contract changes detected in the inspected sample.")
    return recommendations


def _aggregate_schema(records: Any) -> dict[str, str]:
    types: dict[str, set[str]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        for name, value in record.items():
            types.setdefault(name, set()).add(_type_name(value))
    return {name: "|".join(sorted(values)) for name, values in sorted(types.items())}


def _is_compatible_listing_type(name: str, value: Any) -> bool:
    if value is None:
        return name not in {"id", "name"}
    if name in INTEGER_FIELDS:
        return _integer_value(value) is not None
    if name in DECIMAL_FIELDS:
        return _decimal_value(value) is not None
    if name in STRING_FIELDS:
        return isinstance(value, str | int | float) and not isinstance(value, bool)
    if name in ARRAY_FIELDS:
        return isinstance(value, list)
    return True


def _integer_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _decimal_value(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
