"""Serialize authoritative provider IDs across the independent payment ledgers."""

from django.db import connection


def payment_id_conflict(identifier, attempt):
    from .models import DayUsePaymentAttempt, MorningPaymentAttempt, PaymentAttempt

    if not isinstance(identifier, str) or not identifier:
        return False
    if connection.vendor == "postgresql":
        # Every caller holds a transaction through the verified attempt save.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["hyperpay:payment:" + identifier],
            )
    for model in (PaymentAttempt, MorningPaymentAttempt, DayUsePaymentAttempt):
        rows = model.objects.filter(provider_payment_id=identifier)
        if isinstance(attempt, model):
            rows = rows.exclude(pk=attempt.pk)
        if rows.exists():
            return True
    return False
