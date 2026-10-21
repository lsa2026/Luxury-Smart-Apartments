"""Separate payment ledger for morning stays, with no BookingIntent dependency."""

import uuid

from django.db import models
from django.db.models import Q


class MorningPaymentAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stay = models.ForeignKey(
        "reservations.MorningStay", on_delete=models.PROTECT, related_name="attempts"
    )
    merchant_transaction_id = models.CharField(max_length=255, unique=True, editable=False)
    provider_checkout_id = models.CharField(max_length=255, unique=True, null=True, editable=False)
    provider_reference = models.CharField(max_length=255, blank=True, editable=False)
    provider_payment_id = models.CharField(max_length=255, unique=True, null=True, editable=False)
    provider_result_code = models.CharField(max_length=100, blank=True, editable=False)
    widget_integrity = models.CharField(max_length=255, blank=True, editable=False)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="SAR", editable=False)
    status = models.CharField(max_length=12, default="created", editable=False)
    failure_code = models.CharField(max_length=100, blank=True, editable=False)
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0) & Q(currency="SAR"), name="morning_payment_positive_sar"
            ),
            models.UniqueConstraint(
                fields=["stay"],
                condition=Q(status__in=["created", "pending", "unknown", "review", "succeeded"]),
                name="one_unresolved_morning_payment",
            ),
        ]

    def __str__(self):
        return f"Morning payment {self.pk}: {self.status}"
