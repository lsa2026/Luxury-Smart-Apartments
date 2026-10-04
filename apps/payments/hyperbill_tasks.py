"""Bounded, retryable reads; never retry invoice creation or WhatsApp sends."""

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.integrations.tasks import distributed_task_lock

from .hyperbill import HyperBillClient, HyperBillError, reconcile_invoice, require_sandbox
from .models import HyperBillInvoice, HyperBillWebhookSignal


@shared_task(name="apps.payments.hyperbill_tasks.reconcile_hyperbill_task")
def reconcile_hyperbill_task():
    if not settings.HYPERBILL_ENABLED:
        return {"status": "disabled"}
    try:
        require_sandbox()
    except HyperBillError:
        return {"status": "not_uat"}
    with distributed_task_lock("hyperbill-sandbox-reconcile") as acquired:
        if not acquired:
            return {"status": "already_running"}
        started = timezone.now()
        invoices = list(
            HyperBillInvoice.objects.exclude(status__in=["paid", "canceled"])
            .order_by("updated_at")
            .values_list("pk", flat=True)[:25]
        )
        results = []
        try:
            with HyperBillClient() as client:
                for pk in invoices:
                    results.append(reconcile_invoice(pk, client=client).code)
        except HyperBillError:
            return {"status": "provider_unavailable"}
        if any(code.startswith("hyperbill_") for code in results):
            return {"status": "retry_required"}
        if len(invoices) < 25:
            HyperBillWebhookSignal.objects.filter(
                processed_at__isnull=True, created_at__lte=started
            ).update(processed_at=timezone.now())
        return {"status": "reconciled", "checked": len(invoices)}
