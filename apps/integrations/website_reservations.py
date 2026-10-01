"""Refresh only known website reservations; never import OTA reservations."""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.integrations.hostaway.webhook_processor import sync_reservation_snapshot
from apps.integrations.models import IntegrationSyncRun
from apps.integrations.monitoring import alert_operations
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)


def refresh_website_reservations(*, limit: int = 20, for_page: bool = False) -> dict:
    if not settings.HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED:
        return {"status": "disabled", "processed": 0}
    if for_page and not cache.add("lsa:owner-bookings:refresh", "running", timeout=60):
        return {"status": "recently_checked", "processed": 0}
    candidates = list(
        Reservation.objects.filter(
            booking_intent__isnull=False,
            hostaway_reservation_id__isnull=False,
            check_out__gte=timezone.localdate(),
        )
        .exclude(normalized_status__in=Reservation.CLOSED_STATUSES)
        .exclude(last_synced_at__gte=timezone.now() - timedelta(seconds=60))
        .select_related("property")
        .order_by("last_synced_at", "created_at")[: max(1, min(limit, 100))]
    )
    if not candidates:
        return {"status": "completed", "processed": 0}

    def fetch(reservation):
        try:
            with HostawayClient(timeout=2 if for_page else 5, max_get_attempts=1) as client:
                snapshot = client.get_reservation(reservation.hostaway_reservation_id)
            if (
                snapshot.reservation_id != reservation.hostaway_reservation_id
                or snapshot.listing_map_id != reservation.hostaway_listing_map_id
            ):
                return reservation, None
            # Preserve the documented listing currency correction for Marrakech.
            override = reservation.property.price_currency_override if reservation.property else ""
            if override:
                snapshot = replace(snapshot, currency=override)
            return reservation, snapshot
        except HostawayError as exc:
            logger.warning("Website status refresh deferred: code=%s", type(exc).__name__)
            return reservation, None

    now = timezone.now()
    failures = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, candidates))
    # Database writes happen outside the fetch threads and keep row-lock semantics.
    for reservation, snapshot in results:
        if snapshot is None:
            failures += 1
        else:
            with transaction.atomic():
                current = Reservation.objects.select_for_update().get(pk=reservation.pk)
                if current.updated_at != reservation.updated_at:
                    continue  # A concurrent local modification/cancellation wins.
                sync_reservation_snapshot(snapshot)
    if not for_page:
        IntegrationSyncRun.objects.create(
            sync_type=IntegrationSyncRun.SyncType.WEBSITE_RESERVATIONS,
            status="partially_succeeded" if failures else "succeeded",
            started_at=now,
            completed_at=timezone.now(),
            fetched_count=len(candidates),
            updated_count=len(candidates) - failures,
            failed_count=failures,
            error_summary="status_refresh_deferred" if failures else "",
        )
    if failures:
        alert_operations(f"website-status-check:{timezone.localdate()}")
    return {"status": "deferred" if failures else "completed", "processed": len(candidates)}
