"""DTOs and validation for Hostaway listing data."""

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar
from urllib.parse import urlparse

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .exceptions import HostawayResponseError

T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HostawayListingImage:
    image_id: int | None
    url: str
    caption: str
    sort_order: int
    source_updated_at: datetime | None

    @property
    def sync_key(self) -> str:
        if self.image_id is not None:
            return f"id:{self.image_id}"
        return f"url:{hashlib.sha256(self.url.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class HostawayListingAmenity:
    amenity_id: int
    sort_order: int


@dataclass(frozen=True, slots=True)
class HostawayAmenityDefinition:
    amenity_id: int
    name: str


@dataclass(frozen=True, slots=True)
class HostawayCollectionPage:
    """A read-only view of the standard Hostaway collection envelope."""

    records: tuple[dict[str, Any], ...]
    status: Any
    limit: Any
    offset: Any
    count: Any
    page: Any
    total_pages: Any
    fields: frozenset[str]


@dataclass(frozen=True, slots=True)
class HostawayObjectDocument:
    """A read-only view of a Hostaway single-object envelope."""

    record: dict[str, Any]
    status: Any
    fields: frozenset[str]


@dataclass(frozen=True, slots=True)
class HostawayListing:
    listing_map_id: int
    name: str
    description: str
    internal_name: str
    hostaway_property_type_id: int | None
    room_type: str
    person_capacity: int | None
    bedrooms_number: int | None
    beds_number: int | None
    bathrooms_number: Decimal | None
    address: str
    public_address: str
    city: str
    state: str
    country: str
    country_code: str
    zipcode: str
    latitude: Decimal | None
    longitude: Decimal | None
    currency_code: str
    average_review_rating: Decimal | None
    special_status: str
    source_updated_at: datetime | None
    images: tuple[HostawayListingImage, ...]
    amenities: tuple[HostawayListingAmenity, ...]
    images_present: bool
    amenities_present: bool
    validation_errors: tuple[str, ...]

    @property
    def is_active(self) -> bool:
        return self.special_status.strip().casefold() != "archived"


def validate_collection_response(
    payload: Any,
) -> tuple[list[dict[str, Any]], int | None, int | None]:
    """Validate a Hostaway collection response and its pagination metadata."""
    document = inspect_collection_response(payload)
    page = _optional_metadata_integer(document.page, "page")
    total_pages = _optional_metadata_integer(document.total_pages, "totalPages")
    return list(document.records), page, total_pages


def inspect_collection_response(payload: Any) -> HostawayCollectionPage:
    """Inspect an envelope while retaining metadata types for compatibility checks."""
    if not isinstance(payload, dict) or payload.get("status") not in (None, "success"):
        raise HostawayResponseError("Hostaway collection response reports a failure.")
    result = payload.get("result")
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise HostawayResponseError("Hostaway collection response must contain an object list.")
    return HostawayCollectionPage(
        records=tuple(result),
        status=payload.get("status"),
        limit=payload.get("limit"),
        offset=payload.get("offset"),
        count=payload.get("count"),
        page=payload.get("page"),
        total_pages=payload.get("totalPages"),
        fields=frozenset(payload),
    )


def validate_object_response(payload: Any) -> dict[str, Any]:
    """Validate a Hostaway single-object response."""
    return inspect_object_response(payload).record


def inspect_object_response(payload: Any) -> HostawayObjectDocument:
    """Inspect a single-object envelope without retaining it outside memory."""
    if not isinstance(payload, dict) or payload.get("status") not in (None, "success"):
        raise HostawayResponseError("Hostaway object response reports a failure.")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise HostawayResponseError("Hostaway object response must contain an object.")
    return HostawayObjectDocument(
        record=result,
        status=payload.get("status"),
        fields=frozenset(payload),
    )


def normalize_listing(payload: dict[str, Any]) -> HostawayListing:
    """Normalize one listing while isolating optional-field and child errors."""
    listing_map_id = _positive_integer(
        payload.get("listingMapId", payload.get("id")),
        "listingMapId",
    )
    errors: list[str] = []

    images_present = isinstance(payload.get("listingImages"), list)
    images = _normalize_images(
        payload.get("listingImages") if images_present else [],
        errors,
    )
    amenities_present = isinstance(payload.get("listingAmenities"), list)
    amenities = _normalize_listing_amenities(
        payload.get("listingAmenities") if amenities_present else [],
        errors,
    )

    country_code = _safe_optional(
        lambda: _code(payload.get("countryCode"), 2, "countryCode"),
        "",
        errors,
        "countryCode",
    )
    currency_code = _safe_optional(
        lambda: _code(payload.get("currencyCode"), 3, "currencyCode"),
        "",
        errors,
        "currencyCode",
    )
    special_status = _string(payload.get("specialStatus"))
    if special_status and special_status.strip().casefold() != "archived":
        logger.warning(
            "Hostaway listing uses an unknown specialStatus value: %s",
            special_status,
        )

    return HostawayListing(
        listing_map_id=listing_map_id,
        name=_string(payload.get("name")),
        description=_string(payload.get("description")),
        internal_name=_string(payload.get("internalListingName")),
        hostaway_property_type_id=_safe_optional(
            lambda: _optional_nonnegative_integer(payload.get("propertyTypeId"), "propertyTypeId"),
            None,
            errors,
            "propertyTypeId",
        ),
        room_type=_string(payload.get("roomType")),
        person_capacity=_safe_optional(
            lambda: _optional_nonnegative_integer(payload.get("personCapacity"), "personCapacity"),
            None,
            errors,
            "personCapacity",
        ),
        bedrooms_number=_safe_optional(
            lambda: _optional_nonnegative_integer(
                payload.get("bedroomsNumber"),
                "bedroomsNumber",
            ),
            None,
            errors,
            "bedroomsNumber",
        ),
        beds_number=_safe_optional(
            lambda: _optional_nonnegative_integer(payload.get("bedsNumber"), "bedsNumber"),
            None,
            errors,
            "bedsNumber",
        ),
        bathrooms_number=_safe_optional(
            lambda: _optional_decimal(
                payload.get("bathroomsNumber"),
                "bathroomsNumber",
                minimum=Decimal("0"),
            ),
            None,
            errors,
            "bathroomsNumber",
        ),
        address=_string(payload.get("address")),
        public_address=_string(payload.get("publicAddress")),
        city=_string(payload.get("city")),
        state=_string(payload.get("state")),
        country=_string(payload.get("country")),
        country_code=country_code,
        zipcode=_string(payload.get("zipcode")),
        latitude=_safe_optional(
            lambda: _optional_decimal(
                payload.get("lat"),
                "lat",
                minimum=Decimal("-90"),
                maximum=Decimal("90"),
                places=6,
            ),
            None,
            errors,
            "lat",
        ),
        longitude=_safe_optional(
            lambda: _optional_decimal(
                payload.get("lng"),
                "lng",
                minimum=Decimal("-180"),
                maximum=Decimal("180"),
                places=6,
            ),
            None,
            errors,
            "lng",
        ),
        currency_code=currency_code,
        average_review_rating=_safe_optional(
            lambda: _optional_decimal(
                payload.get("averageReviewRating"),
                "averageReviewRating",
                minimum=Decimal("0"),
                maximum=Decimal("10"),
            ),
            None,
            errors,
            "averageReviewRating",
        ),
        special_status=special_status,
        source_updated_at=_safe_optional(
            lambda: _optional_datetime(
                payload.get("updatedOn") or payload.get("latestActivityOn"),
                "updatedOn/latestActivityOn",
            ),
            None,
            errors,
            "updatedOn/latestActivityOn",
        ),
        images=tuple(images),
        amenities=tuple(amenities),
        images_present=images_present,
        amenities_present=amenities_present,
        validation_errors=tuple(errors),
    )


def normalize_amenity_definitions(
    payloads: list[dict[str, Any]],
) -> tuple[dict[int, HostawayAmenityDefinition], list[str]]:
    definitions: dict[int, HostawayAmenityDefinition] = {}
    errors: list[str] = []
    for index, payload in enumerate(payloads):
        try:
            amenity_id = _positive_integer(payload.get("id"), "id")
            definitions[amenity_id] = HostawayAmenityDefinition(
                amenity_id=amenity_id,
                name=_string(payload.get("name")),
            )
        except HostawayResponseError as exc:
            errors.append(f"amenity definition {index}: {exc}")
    return definitions, errors


def _normalize_images(
    payloads: list[Any],
    errors: list[str],
) -> list[HostawayListingImage]:
    images: list[HostawayListingImage] = []
    seen_keys: set[str] = set()
    for index, payload in enumerate(payloads):
        try:
            if not isinstance(payload, dict):
                raise HostawayResponseError("must be an object")
            image = HostawayListingImage(
                image_id=_optional_positive_integer(payload.get("id"), "id"),
                url=_https_url(payload.get("url")),
                caption=_string(payload.get("caption")),
                sort_order=_optional_nonnegative_integer(
                    payload.get("sortOrder"),
                    "sortOrder",
                )
                or 0,
                source_updated_at=_optional_datetime(
                    payload.get("updatedOn"),
                    "updatedOn",
                ),
            )
            if image.sync_key in seen_keys:
                errors.append(f"image {index}: duplicate source key")
                continue
            seen_keys.add(image.sync_key)
            images.append(image)
        except HostawayResponseError as exc:
            errors.append(f"image {index}: {exc}")
    return images


def _normalize_listing_amenities(
    payloads: list[Any],
    errors: list[str],
) -> list[HostawayListingAmenity]:
    amenities: list[HostawayListingAmenity] = []
    seen_ids: set[int] = set()
    for index, payload in enumerate(payloads):
        try:
            if not isinstance(payload, dict):
                raise HostawayResponseError("must be an object")
            amenity_id = _positive_integer(payload.get("amenityId"), "amenityId")
            if amenity_id in seen_ids:
                errors.append(f"listing amenity {index}: duplicate amenityId")
                continue
            seen_ids.add(amenity_id)
            amenities.append(
                HostawayListingAmenity(
                    amenity_id=amenity_id,
                    sort_order=index,
                )
            )
        except HostawayResponseError as exc:
            errors.append(f"listing amenity {index}: {exc}")
    return amenities


def _safe_optional(
    parser: Callable[[], T],
    fallback: T,
    errors: list[str],
    field_name: str,
) -> T:
    try:
        return parser()
    except HostawayResponseError as exc:
        errors.append(f"{field_name}: {exc}")
        return fallback


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str | int | float):
        return str(value)
    return ""


def _positive_integer(value: Any, field_name: str) -> int:
    parsed = _integer(value, field_name)
    if parsed <= 0:
        raise HostawayResponseError(f"{field_name} must be positive")
    return parsed


def _optional_positive_integer(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    return _positive_integer(value, field_name)


def _optional_nonnegative_integer(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    parsed = _integer(value, field_name)
    if parsed < 0:
        raise HostawayResponseError(f"{field_name} cannot be negative")
    return parsed


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise HostawayResponseError(f"{field_name} must be an integer")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HostawayResponseError(f"{field_name} must be an integer") from exc
    if not decimal_value.is_finite():
        raise HostawayResponseError(f"{field_name} must be an integer")
    if decimal_value != decimal_value.to_integral_value():
        raise HostawayResponseError(f"{field_name} must be an integer")
    return int(decimal_value)


def _optional_decimal(
    value: Any,
    field_name: str,
    *,
    minimum: Decimal,
    maximum: Decimal | None = None,
    places: int = 1,
) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HostawayResponseError(f"{field_name} must be numeric") from exc
    if not parsed.is_finite():
        raise HostawayResponseError(f"{field_name} must be numeric")
    try:
        if parsed < minimum or (maximum is not None and parsed > maximum):
            raise HostawayResponseError(f"{field_name} is outside the allowed range")
        quantum = Decimal("1").scaleb(-places)
        return parsed.quantize(quantum)
    except InvalidOperation as exc:
        raise HostawayResponseError(f"{field_name} must be numeric") from exc


def _code(value: Any, length: int, field_name: str) -> str:
    parsed = _string(value).upper()
    if not parsed:
        return ""
    if not re.fullmatch(rf"[A-Z]{{{length}}}", parsed):
        raise HostawayResponseError(f"{field_name} has an invalid format")
    return parsed


def _https_url(value: Any) -> str:
    parsed_value = _string(value).strip()
    parsed = urlparse(parsed_value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or len(parsed_value) > 2048
    ):
        raise HostawayResponseError("image URL must be a safe HTTPS URL")
    return parsed_value


def _optional_datetime(value: Any, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise HostawayResponseError(f"{field_name} must be a datetime string")
    parsed = parse_datetime(value)
    if parsed is None:
        raise HostawayResponseError(f"{field_name} is invalid")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
    return parsed


def _optional_metadata_integer(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    parsed = _integer(value, field_name)
    if parsed < 0:
        raise HostawayResponseError(f"{field_name} cannot be negative")
    return parsed
