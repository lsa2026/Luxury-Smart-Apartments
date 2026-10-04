"""Empty POST webhook, authenticated by an unguessable callback path."""

import logging

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .hyperbill import HyperBillError, require_sandbox
from .hyperbill_tasks import reconcile_hyperbill_task
from .models import HyperBillWebhookSignal

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def hyperbill_webhook(request, secret):
    try:
        require_sandbox()
    except HyperBillError:
        return HttpResponse(status=404)
    if len(settings.HYPERBILL_WEBHOOK_SECRET) < 32 or not constant_time_compare(
        secret, settings.HYPERBILL_WEBHOOK_SECRET
    ):
        return HttpResponse(status=404)
    # No JSON/body is needed. All payment facts are retrieved server-to-server.
    # Coalesce wakeups; periodic polling also recovers missed notifications.
    if cache.add("hyperbill-webhook-wakeup", True, timeout=10):
        HyperBillWebhookSignal.objects.create()
        if settings.HYPERBILL_RECONCILIATION_ENABLED:
            try:
                reconcile_hyperbill_task.delay()
            except Exception:
                # Durable signal survives a queue outage; don't invite repeat payments.
                logger.warning("HyperBill signal saved; dispatch unavailable")
    return HttpResponse(status=200)
