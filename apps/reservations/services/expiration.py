"""Local expiration services; never contact Hostaway or payment providers."""

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.reservations.models import (
    BookingIntent,
    BookingModificationRequest,
    BookingQuote,
)


@dataclass(frozen=True, slots=True)
class BookingExpirationResult:
    quotes: int
    intents: int
    modifications: int


def expire_booking_objects(
    *,
    dry_run: bool = False,
    limit: int = 500,
    include_prebooking: bool = True,
    include_modifications: bool = True,
) -> BookingExpirationResult:
    """Expire bounded local records without deleting PII or touching external systems."""
    if not 1 <= limit <= 5000:
        raise ValueError("limit must be between 1 and 5000.")
    now = timezone.now()
    quote_ids = (
        list(
            BookingQuote.objects.filter(
                status=BookingQuote.Status.ACTIVE,
                expires_at__lte=now,
            )
            .order_by("expires_at")
            .values_list("pk", flat=True)[:limit]
        )
        if include_prebooking
        else []
    )
    intent_ids = (
        list(
            BookingIntent.objects.filter(expires_at__lte=now)
            .exclude(
                status__in=(
                    BookingIntent.Status.COMPLETED,
                    BookingIntent.Status.CANCELLED,
                    BookingIntent.Status.EXPIRED,
                )
            )
            .order_by("expires_at")
            .values_list("pk", flat=True)[:limit]
        )
        if include_prebooking
        else []
    )
    terminal_modifications = (
        BookingModificationRequest.Status.COMPLETED,
        BookingModificationRequest.Status.REJECTED,
        BookingModificationRequest.Status.EXPIRED,
    )
    modification_ids = (
        list(
            BookingModificationRequest.objects.filter(expires_at__lte=now)
            .exclude(status__in=terminal_modifications)
            .order_by("expires_at")
            .values_list("pk", flat=True)[:limit]
        )
        if include_modifications
        else []
    )
    result = BookingExpirationResult(
        quotes=len(quote_ids),
        intents=len(intent_ids),
        modifications=len(modification_ids),
    )
    if dry_run:
        return result
    with transaction.atomic():
        BookingQuote.objects.filter(pk__in=quote_ids).update(
            status=BookingQuote.Status.EXPIRED,
            updated_at=now,
        )
        BookingIntent.objects.filter(pk__in=intent_ids).update(
            status=BookingIntent.Status.EXPIRED,
            updated_at=now,
        )
        BookingModificationRequest.objects.filter(pk__in=modification_ids).update(
            status=BookingModificationRequest.Status.EXPIRED,
            updated_at=now,
        )
    return result
