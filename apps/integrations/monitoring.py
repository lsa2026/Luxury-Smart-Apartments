"""Real run receipts and bounded, deduplicated operations alerts."""

import logging
import re
from datetime import timedelta
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.integrations.models import IntegrationSyncRun
from apps.notifications.services.email import queue_email
from apps.notifications.services.events import dispatch_event

logger = logging.getLogger(__name__)


def alert_operations(key: str, *, reference: str = "") -> None:
    # An alert is secondary, including when its own database/provider is down.
    # It must never turn a verified payment into a failed guest request.
    try:
        dispatch_event(
            "hostaway_sync.failed",
            event_key=key,
            related_object_type="IntegrationSyncRun",
            related_object_reference=reference or key[:100],
            action_url="/admin/integrations/integrationsyncrun/health/",
        )
    except Exception:
        logger.exception("Could not persist integration alert")
    if settings.EMAIL_DELIVERY_ENABLED and settings.OPERATIONS_EMAIL:
        try:
            queue_email(
                message_type=(
                    "hostaway_refund_accounting_attention"
                    if key.startswith("hostaway-refund-accounting:")
                    else "integration_attention"
                ),
                recipient=settings.OPERATIONS_EMAIL,
                recipient_source="operations",
                recipient_reference=reference or key[:100],
                language="ar",
                idempotency_key=key[:100],
            )
        except Exception:
            logger.exception("Could not queue integration alert")


def run_display_command(command: str, sync_type: str) -> dict[str, str]:
    now = timezone.now()
    IntegrationSyncRun.objects.filter(
        sync_type=sync_type, status="running", started_at__lt=now - timedelta(hours=1)
    ).update(status="failed", completed_at=now, error_summary="interrupted_run")
    try:
        with transaction.atomic():
            run = IntegrationSyncRun.objects.create(
                sync_type=sync_type, status="running", started_at=now
            )
    except IntegrityError:
        return {"status": "already_running"}
    output = StringIO()
    try:
        call_command(command, stdout=output, stderr=output)
        summary = output.getvalue()
        match = re.search(r"failed[=:]\s*(\d+)", summary)
        failures = int(match.group(1)) if match else 0
        old_match = re.search(r"(\d+) failed\.", summary)
        failures = max(failures, int(old_match.group(1)) if old_match else 0)
        run.failed_count = failures
        run.status = "failed" if failures else "succeeded"
        run.error_summary = "provider_fetch_failed" if failures else ""
    except Exception as exc:
        run.status = "failed"
        run.failed_count = 1
        run.error_summary = type(exc).__name__
        logger.exception("Display sync failed: command=%s", command)
    run.completed_at = timezone.now()
    run.save()
    if run.status == "failed":
        alert_operations(f"sync-failed:{run.pk}", reference=str(run.pk))
    return {"status": run.status}


def check_daily_sync_health() -> None:
    from apps.payments.hostaway_ledger import _dispatch_receipt
    from apps.payments.models import HostawayFinancialEntry

    # Durable pending receipt requests can be redispatched safely. Unknown POST
    # outcomes are never automatically reissued; those need manual review.
    if settings.HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED:
        for entry in HostawayFinancialEntry.objects.filter(
            status="pending", created_at__lt=timezone.now() - timedelta(minutes=10)
        )[:20]:
            _dispatch_receipt(entry.pk)
        for entry in HostawayFinancialEntry.objects.filter(
            status="posting", updated_at__lt=timezone.now() - timedelta(minutes=15)
        )[:20]:
            alert_operations(f"receipt-interrupted:{entry.pk}")
    for enabled, kind in (
        (settings.HOSTAWAY_PRICE_CALENDAR_SYNC_ENABLED, "price_calendar"),
        (settings.TRUSTINDEX_REVIEW_SYNC_ENABLED, "trustindex_metrics"),
    ):
        if not enabled:
            continue
        latest = IntegrationSyncRun.objects.filter(sync_type=kind, status="succeeded").first()
        if latest is None or latest.completed_at < timezone.now() - timedelta(hours=30):
            alert_operations(f"sync-stale:{kind}:{timezone.localdate()}")
