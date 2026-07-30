"""Read-only integration health projection for Django Admin."""

from dataclasses import dataclass

from celery import current_app
from django.conf import settings
from django.core.cache import cache
from kombu.exceptions import KombuError
from redis.exceptions import RedisError

from apps.integrations.models import IntegrationSyncRun
from apps.properties.models import Property


@dataclass(frozen=True, slots=True)
class IntegrationHealth:
    last_property_sync: IntegrationSyncRun | None
    last_review_sync: IntegrationSyncRun | None
    local_properties: int
    active_properties: int
    pending_review: int
    manually_hidden: int
    archived: int
    redis_status: str
    worker_status: str
    beat_status: str
    authentication_status: str
    write_flags: dict[str, bool]


def get_integration_health() -> IntegrationHealth:
    """Collect bounded local/configuration checks without calling Hostaway."""
    redis_status = "not_configured"
    if settings.CACHE_URL:
        try:
            cache.set("lsa:health:cache", "ok", timeout=10)
            redis_status = "available" if cache.get("lsa:health:cache") == "ok" else "unavailable"
        except (ConnectionError, OSError, TimeoutError, RedisError):
            redis_status = "unavailable"

    worker_status = "dispatch_disabled"
    if settings.CELERY_SYNC_DISPATCH_ENABLED:
        try:
            replies = current_app.control.inspect(timeout=0.5).ping() or {}
            worker_status = "available" if replies else "unavailable"
        except (ConnectionError, OSError, TimeoutError, KombuError):
            worker_status = "unavailable"

    authentication_status = (
        "configured"
        if settings.HOSTAWAY_ACCESS_TOKEN
        or (settings.HOSTAWAY_ACCOUNT_ID and settings.HOSTAWAY_API_SECRET)
        else "not_configured"
    )
    return IntegrationHealth(
        last_property_sync=IntegrationSyncRun.objects.filter(
            sync_type=IntegrationSyncRun.SyncType.HOSTAWAY_PROPERTIES
        ).first(),
        last_review_sync=IntegrationSyncRun.objects.filter(
            sync_type=IntegrationSyncRun.SyncType.HOSTAWAY_REVIEWS
        ).first(),
        local_properties=Property.objects.count(),
        active_properties=Property.objects.filter(hostaway_is_active=True).count(),
        pending_review=Property.objects.filter(
            visibility_management=Property.VisibilityManagement.AUTOMATIC,
            is_visible=False,
            source_missing=False,
        ).count(),
        manually_hidden=Property.objects.filter(
            visibility_management=Property.VisibilityManagement.MANUAL,
            is_visible=False,
        ).count(),
        archived=Property.objects.filter(hostaway_is_active=False).count(),
        redis_status=redis_status,
        worker_status=worker_status,
        beat_status="enabled" if settings.HOSTAWAY_AUTO_SYNC_ENABLED else "disabled",
        authentication_status=authentication_status,
        write_flags={
            "live_booking": settings.HOSTAWAY_LIVE_BOOKING_ENABLED,
            "live_modification": settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED,
            "live_extension": settings.HOSTAWAY_LIVE_EXTENSION_ENABLED,
            "live_cancellation": settings.HOSTAWAY_LIVE_CANCELLATION_ENABLED,
            "webhook_receiver": settings.HOSTAWAY_WEBHOOK_RECEIVER_ENABLED,
            "webhook_processing": settings.HOSTAWAY_WEBHOOK_PROCESSING_ENABLED,
        },
    )
