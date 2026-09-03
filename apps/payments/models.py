"""Provider-neutral payment records; none are created automatically."""

import uuid

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy


class PaymentAttempt(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", _("Created")
        PENDING = "pending", _("Processing")
        SUCCEEDED = "succeeded", _("Successful")
        FAILED = "failed", pgettext_lazy("PaymentAttempt", "Failed")
        CANCELLED = "cancelled", pgettext_lazy("PaymentAttempt", "Cancelled")
        EXPIRED = "expired", pgettext_lazy("PaymentAttempt", "Expired")
        REFUNDED = "refunded", _("Refunded")
        PARTIALLY_REFUNDED = "partially_refunded", _("Partially refunded")
        REVIEW = "review", _("Needs review")
        UNKNOWN = "unknown", pgettext_lazy("PaymentAttempt", "Unknown")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking_intent = models.ForeignKey(
        "reservations.BookingIntent",
        on_delete=models.PROTECT,
        related_name="payment_attempts",
    )
    modification_request = models.ForeignKey(
        "reservations.BookingModificationRequest",
        on_delete=models.PROTECT,
        related_name="payment_attempts",
        null=True,
        blank=True,
    )
    provider = models.CharField(max_length=50)
    provider_reference = models.CharField(max_length=255, null=True, blank=True)
    provider_checkout_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    provider_payment_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    merchant_transaction_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    widget_integrity = models.CharField(max_length=255, blank=True, editable=False)
    provider_result_code = models.CharField(max_length=100, blank=True, editable=False)
    provider_result_description = models.CharField(max_length=255, blank=True, editable=False)
    amount = models.DecimalField(max_digits=14, decimal_places=4)
    currency = models.CharField(max_length=3)
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.CREATED,
    )
    idempotency_key = models.CharField(max_length=64, unique=True)
    failure_code = models.CharField(max_length=100, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["booking_intent", "status"]),
            models.Index(fields=["provider", "provider_reference"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="payment_attempt_amount_nonnegative",
            )
        ]
        verbose_name = _("Payment attempt")
        verbose_name_plural = _("Payment attempts")

    def __str__(self) -> str:
        return f"{self.provider} — {self.amount} {self.currency}"
