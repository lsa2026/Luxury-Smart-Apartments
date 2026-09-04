"""Celery orchestration for existing Hostaway and expiration services."""

import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.property_services import sync_properties
from apps.integrations.hostaway.services import sync_reviews
from apps.integrations.hostaway.webhook_processor import (
    claim_webhook_event_ids,
    process_webhook_event,
)
from apps.reservations.services.expiration import expire_booking_objects

logger = logging.getLogger(__name__)


@contextmanager
def distributed_task_lock(name: str, *, timeout: int = 10 * 60) -> Iterator[bool]:
    """Use the configured shared cache as a distributed production lock."""
    token = secrets.token_urlsafe(18)
    key = f"lsa:task-lock:{name}"
    acquired = cache.add(key, token, timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired and cache.get(key) == token:
            cache.delete(key)


@shared_task(
    name="apps.integrations.tasks.sync_hostaway_properties_task",
    soft_time_limit=9 * 60,
    time_limit=10 * 60,
)
def sync_hostaway_properties_task(
    listing_id: int | None = None,
    dry_run: bool = False,
) -> dict[str, int | str]:
    """Run the read-from-Hostaway property sync once."""
    with distributed_task_lock("properties") as acquired:
        if not acquired:
            return {"status": "already_running"}
        report = sync_properties(listing_id=listing_id, dry_run=dry_run)
    return {
        "status": "completed",
        "fetched": report.fetched,
        "created": report.properties_created,
        "updated": report.properties_updated,
        "failed": report.properties_failed,
    }


@shared_task(
    name="apps.integrations.tasks.sync_hostaway_reviews_task",
    soft_time_limit=9 * 60,
    time_limit=10 * 60,
)
def sync_hostaway_reviews_task(
    listing_id: int | None = None,
    dry_run: bool = False,
) -> dict[str, int | str]:
    """Run the published guest-to-host review sync once."""
    with distributed_task_lock("reviews") as acquired:
        if not acquired:
            return {"status": "already_running"}
        report = sync_reviews(listing_id=listing_id, dry_run=dry_run)
    return {
        "status": "completed",
        "fetched": report.fetched,
        "created": report.created,
        "updated": report.updated,
        "failed": report.failed,
    }


@shared_task(
    name="apps.integrations.tasks.process_hostaway_webhooks_task",
    soft_time_limit=50,
    time_limit=60,
)
def process_hostaway_webhooks_task(limit: int = 50) -> dict[str, int | str]:
    """Process already-sanitized local events only when processing is enabled."""
    if not settings.HOSTAWAY_WEBHOOK_PROCESSING_ENABLED:
        return {"status": "disabled", "processed": 0}
    ids = claim_webhook_event_ids(limit=max(1, min(limit, 100)))
    processed = 0
    with HostawayClient() as client:
        for event_id in ids:
            result = process_webhook_event(event_id, client=client)
            processed += int(result.code in {"processed", "stale"})
    return {"status": "completed", "processed": processed}


@shared_task(
    name="apps.integrations.tasks.expire_booking_objects_task",
    soft_time_limit=50,
    time_limit=60,
)
def expire_booking_objects_task() -> dict[str, Any]:
    """Expire local objects without Hostaway or payment calls."""
    result = expire_booking_objects()
    return {
        "status": "completed",
        "quotes": result.quotes,
        "intents": result.intents,
        "modifications": result.modifications,
    }


@shared_task(
    name="apps.integrations.tasks.reconcile_paid_hostaway_reservations_task",
    soft_time_limit=9 * 60,
    time_limit=10 * 60,
)
def reconcile_paid_hostaway_reservations_task(limit: int = 20) -> dict[str, int | str]:
    """Recover verified payments that have not yet reached Hostaway.

    Missing Listing Map IDs are first learned from Hostaway's own reservation
    records. The booking service then supplies the existing row-level locking
    and operation ledger, so overlapping result-page requests or task runs can
    never issue a second reservation POST.
    """
    if not settings.HOSTAWAY_LIVE_BOOKING_ENABLED:
        return {"status": "disabled", "processed": 0, "confirmed": 0}
    safe_limit = max(1, min(int(limit), 100))
    with distributed_task_lock("paid-hostaway-reservations", timeout=10 * 60) as acquired:
        if not acquired:
            return {"status": "already_running", "processed": 0, "confirmed": 0}

        backfill_status = "completed"
        try:
            # The command is deliberately PII-free and only accepts an ID when
            # every sampled reservation for the listing agrees on it.
            call_command(
                "backfill_hostaway_listing_map_ids",
                sample=100,
                stdout=StringIO(),
                stderr=StringIO(),
            )
        except CommandError as exc:
            backfill_status = "failed"
            logger.warning(
                "Hostaway Listing Map ID recovery failed: code=%s",
                type(exc).__name__,
            )

        from apps.reservations.models import Reservation
        from apps.reservations.services.hostaway_booking import HostawayBookingService

        reservations = list(
            Reservation.objects.filter(
                normalized_status=Reservation.Status.READY_FOR_HOSTAWAY,
                payment_status="paid",
                hostaway_reservation_id__isnull=True,
                booking_intent__isnull=False,
            )
            .select_related("booking_intent", "property")
            .order_by("created_at")[:safe_limit]
        )
        confirmed = 0
        deferred = 0
        failed = 0
        with HostawayBookingService() as service:
            for reservation in reservations:
                try:
                    outcome = service.create_hostaway_reservation(reservation)
                except Exception:  # pragma: no cover - defensive task boundary
                    failed += 1
                    logger.exception(
                        "Paid Hostaway reservation recovery crashed: reservation_ref=%s",
                        reservation.public_reference[:8],
                    )
                    continue
                if outcome.code in {"confirmed", "already_confirmed"}:
                    confirmed += 1
                else:
                    deferred += 1
                    logger.warning(
                        "Paid Hostaway reservation recovery deferred: "
                        "reservation_ref=%s code=%s",
                        reservation.public_reference[:8],
                        outcome.code,
                    )
        return {
            "status": "completed" if failed == 0 else "partially_completed",
            "backfill": backfill_status,
            "processed": len(reservations),
            "confirmed": confirmed,
            "deferred": deferred,
            "failed": failed,
        }
