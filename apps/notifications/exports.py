"""Allowlisted, injection-safe operational CSV exports."""

import csv
from collections.abc import Iterable

from apps.core.models import ContactMessage
from apps.integrations.models import IntegrationSyncRun
from apps.properties.models import Property
from apps.reservations.models import BookingIntent, BookingModificationRequest, BookingQuote
from apps.reviews.models import Review


def csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text.replace("\r", " ").replace("\n", " ")


def report_rows(report: str) -> tuple[list[str], Iterable[Iterable[object]]]:
    if report == "properties":
        rows = Property.objects.order_by("hostaway_listing_id").values_list(
            "name_ar",
            "name_en",
            "city_ar",
            "city_en",
            "currency_code",
            "person_capacity",
            "is_visible",
            "hostaway_is_active",
        )
        return [
            "name_ar",
            "name_en",
            "city_ar",
            "city_en",
            "currency",
            "capacity",
            "visible",
            "active",
        ], rows
    if report == "reviews":
        rows = Review.objects.order_by("-departure_date").values_list(
            "property__name_ar",
            "rating",
            "status",
            "is_visible",
            "is_featured",
            "departure_date",
        )
        return ["property", "rating", "status", "visible", "featured", "date"], rows
    if report == "contacts":
        rows = ContactMessage.objects.order_by("-created_at").values_list(
            "status",
            "language",
            "created_at",
        )
        return ["status", "language", "created_at"], rows
    if report == "quotes":
        rows = BookingQuote.objects.order_by("-created_at").values_list(
            "property__name_ar",
            "check_in",
            "check_out",
            "nights",
            "guests",
            "currency",
            "total_price",
            "status",
            "created_at",
        )
        return [
            "property",
            "check_in",
            "check_out",
            "nights",
            "guests",
            "currency",
            "request_value",
            "status",
            "created_at",
        ], rows
    if report == "intents":
        rows = BookingIntent.objects.order_by("-created_at").values_list(
            "public_reference",
            "property__name_ar",
            "check_in",
            "check_out",
            "guests",
            "currency",
            "total_price",
            "status",
            "created_at",
        )
        return [
            "reference",
            "property",
            "check_in",
            "check_out",
            "guests",
            "currency",
            "request_value",
            "status",
            "created_at",
        ], rows
    if report == "modifications":
        rows = BookingModificationRequest.objects.order_by("-created_at").values_list(
            "public_reference",
            "request_type",
            "status",
            "currency",
            "price_difference",
            "created_at",
        )
        return ["reference", "type", "status", "currency", "difference", "created_at"], rows
    if report == "sync_runs":
        rows = IntegrationSyncRun.objects.order_by("-started_at").values_list(
            "sync_type",
            "status",
            "started_at",
            "completed_at",
            "fetched_count",
            "created_count",
            "updated_count",
            "skipped_count",
            "failed_count",
            "dry_run",
        )
        return [
            "type",
            "status",
            "started_at",
            "completed_at",
            "fetched",
            "created",
            "updated",
            "skipped",
            "failed",
            "dry_run",
        ], rows
    raise ValueError("unsupported_report")


def write_csv(response: object, headers: list[str], rows: Iterable[Iterable[object]]) -> None:
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([csv_safe(value) for value in row])
