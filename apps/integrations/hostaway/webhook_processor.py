"""Deferred processing for sanitized Hostaway Unified Webhook events."""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.integrations.models import HostawayWebhookEvent
from apps.properties.models import Property
from apps.reservations.models import Reservation

from .client import HostawayClient
from .exceptions import (
    HostawayAuthenticationError,
    HostawayNetworkError,
    HostawayNotFoundError,
    HostawayRateLimitError,
    HostawayResponseError,
    HostawayServerError,
)
from .reservation_validators import (
    HostawayReservationSnapshot,
    normalize_hostaway_reservation_status,
    source_type_from_snapshot,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebhookProcessingResult:
    code: str
    event_id: object
    reservation_id: object | None = None
    match_strategy: str = "unmatched"


def process_webhook_event(
    event_id: object,
    *,
    client: HostawayClient,
    retry_failed: bool = False,
) -> WebhookProcessingResult:
    event = _claim_event(event_id, retry_failed=retry_failed)
    if event is None:
        return WebhookProcessingResult("not_eligible", event_id)
    if event.event_type not in settings.HOSTAWAY_WEBHOOK_ALLOWED_EVENTS:
        _finish_event(event, HostawayWebhookEvent.Status.IGNORED, "unsupported_event")
        return WebhookProcessingResult("ignored", event.pk)
    if event.hostaway_reservation_id is None:
        _finish_event(event, HostawayWebhookEvent.Status.FAILED, "reservation_id_missing")
        return WebhookProcessingResult("failed", event.pk)
    try:
        snapshot = client.get_reservation(event.hostaway_reservation_id)
    except HostawayNotFoundError:
        _retry_event(event, "hostaway_reservation_temporarily_missing")
        return WebhookProcessingResult("retryable", event.pk)
    except (
        HostawayNetworkError,
        HostawayRateLimitError,
        HostawayServerError,
        HostawayAuthenticationError,
    ):
        _retry_event(event, "hostaway_temporarily_unavailable")
        return WebhookProcessingResult("retryable", event.pk)
    except HostawayResponseError:
        _finish_event(event, HostawayWebhookEvent.Status.FAILED, "reservation_schema_invalid")
        return WebhookProcessingResult("failed", event.pk)

    reservation, strategy, stale = sync_reservation_snapshot(snapshot)
    _finish_event(
        event,
        HostawayWebhookEvent.Status.PROCESSED,
        "stale_event_ignored" if stale else "",
    )
    return WebhookProcessingResult(
        "stale" if stale else "processed",
        event.pk,
        reservation.pk,
        strategy,
    )


def sync_reservation_snapshot(
    snapshot: HostawayReservationSnapshot,
) -> tuple[Reservation, str, bool]:
    property_obj, strategy = _match_property(snapshot.listing_map_id)
    with transaction.atomic():
        existing = (
            Reservation.objects.select_for_update()
            .filter(hostaway_reservation_id=snapshot.reservation_id)
            .first()
        )
        if (
            existing is not None
            and existing.source_updated_at is not None
            and snapshot.updated_at is not None
            and snapshot.updated_at < existing.source_updated_at
        ):
            return existing, strategy, True
        reservation = existing or Reservation(
            hostaway_reservation_id=snapshot.reservation_id,
            public_reference=Reservation._meta.get_field("public_reference").get_default(),
        )
        if reservation.property_id is None:
            reservation.property = property_obj
        if property_obj is not None:
            reservation.hostaway_listing_id = property_obj.hostaway_listing_id
        reservation.hostaway_listing_map_id = snapshot.listing_map_id
        reservation.channel_id = snapshot.channel_id
        if reservation.booking_intent_id is None:
            reservation.source_type = source_type_from_snapshot(snapshot)
        reservation.hostaway_status = snapshot.status
        reservation.normalized_status = normalize_hostaway_reservation_status(snapshot.status)
        reservation.payment_status = snapshot.payment_status
        reservation.check_in = snapshot.check_in
        reservation.check_out = snapshot.check_out
        reservation.nights = (snapshot.check_out - snapshot.check_in).days
        reservation.guests = snapshot.guests
        reservation.currency = snapshot.currency
        reservation.total_price = snapshot.total_price
        reservation.source_updated_at = snapshot.updated_at
        reservation.last_synced_at = timezone.now()
        if reservation.normalized_status == Reservation.Status.CONFIRMED:
            reservation.confirmed_at = reservation.confirmed_at or timezone.now()
            reservation.cancelled_at = None
        elif reservation.normalized_status == Reservation.Status.CANCELLED:
            reservation.cancelled_at = reservation.cancelled_at or timezone.now()
            reservation.confirmed_at = None
        reservation.full_clean()
        reservation.save()
        return reservation, strategy, False


def claim_webhook_event_ids(
    *,
    limit: int,
    event_id: object | None = None,
    retry_failed: bool = False,
) -> list[object]:
    statuses = [
        HostawayWebhookEvent.Status.RECEIVED,
        HostawayWebhookEvent.Status.RETRYABLE,
    ]
    if retry_failed:
        statuses.append(HostawayWebhookEvent.Status.FAILED)
    query = HostawayWebhookEvent.objects.filter(status__in=statuses)
    if event_id is not None:
        query = query.filter(pk=event_id)
    now = timezone.now()
    query = query.filter(next_retry_at__isnull=True) | query.filter(next_retry_at__lte=now)
    with transaction.atomic():
        if connection.features.has_select_for_update_skip_locked:
            query = query.select_for_update(skip_locked=True)
        else:
            query = query.select_for_update()
        return list(query.order_by("received_at").values_list("pk", flat=True)[:limit])


def _claim_event(
    event_id: object,
    *,
    retry_failed: bool,
) -> HostawayWebhookEvent | None:
    allowed = {
        HostawayWebhookEvent.Status.RECEIVED,
        HostawayWebhookEvent.Status.RETRYABLE,
    }
    if retry_failed:
        allowed.add(HostawayWebhookEvent.Status.FAILED)
    with transaction.atomic():
        query = HostawayWebhookEvent.objects
        if connection.features.has_select_for_update_skip_locked:
            query = query.select_for_update(skip_locked=True)
        else:
            query = query.select_for_update()
        event = query.filter(pk=event_id).first()
        if event is None or event.status not in allowed:
            return None
        if event.attempt_count >= settings.HOSTAWAY_WEBHOOK_MAX_PROCESSING_ATTEMPTS:
            event.status = HostawayWebhookEvent.Status.FAILED
            event.error_code = "maximum_attempts_exceeded"
            event.processed_at = timezone.now()
            event.save()
            return None
        event.status = HostawayWebhookEvent.Status.PROCESSING
        event.attempt_count += 1
        event.processing_started_at = timezone.now()
        event.error_code = ""
        event.save()
        return event


def _match_property(listing_map_id: int) -> tuple[Property | None, str]:
    property_obj = Property.objects.filter(hostaway_listing_map_id=listing_map_id).first()
    if property_obj:
        return property_obj, "listing_map_id"
    fallback = list(Property.objects.filter(hostaway_listing_id=listing_map_id)[:2])
    if len(fallback) == 1:
        return fallback[0], "listing_id_fallback"
    return None, "unmatched"


def _retry_event(event: HostawayWebhookEvent, code: str) -> None:
    if event.attempt_count >= settings.HOSTAWAY_WEBHOOK_MAX_PROCESSING_ATTEMPTS:
        _finish_event(event, HostawayWebhookEvent.Status.FAILED, "maximum_attempts_exceeded")
        return
    with transaction.atomic():
        locked = HostawayWebhookEvent.objects.select_for_update().get(pk=event.pk)
        locked.status = HostawayWebhookEvent.Status.RETRYABLE
        locked.error_code = code
        locked.next_retry_at = timezone.now() + timedelta(minutes=5)
        locked.save()
        _log_transition(locked)


def _finish_event(event: HostawayWebhookEvent, status: str, code: str) -> None:
    with transaction.atomic():
        locked = HostawayWebhookEvent.objects.select_for_update().get(pk=event.pk)
        locked.status = status
        locked.error_code = code
        locked.processed_at = timezone.now()
        locked.next_retry_at = None
        locked.save()
        _log_transition(locked)


def _log_transition(event: HostawayWebhookEvent) -> None:
    duration_ms = None
    if event.processing_started_at is not None:
        duration_ms = max(
            0,
            round((timezone.now() - event.processing_started_at).total_seconds() * 1000),
        )
    logger.info(
        "Hostaway webhook transition: event_id=%s event_type=%s status=%s "
        "error_code=%s duration_ms=%s",
        event.pk,
        event.event_type,
        event.status,
        event.error_code or "none",
        duration_ms,
    )
