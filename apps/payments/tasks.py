"""Ensure bookkeeping tasks are registered by Celery autodiscovery."""

from .hostaway_ledger import record_payment_receipt_task  # noqa: F401
