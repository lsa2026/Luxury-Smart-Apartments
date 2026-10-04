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
            ),
            models.CheckConstraint(
                condition=~Q(provider="hyperpay") | Q(currency="SAR"),
                name="hyperpay_payment_currency_sar",
            ),
        ]
        verbose_name = _("Payment attempt")
        verbose_name_plural = _("Payment attempts")

    def __str__(self) -> str:
        return f"{self.provider} — {self.amount} {self.currency}"


class HyperBillInvoice(models.Model):
    """Sandbox evidence, deliberately separate from real collections/refunds."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.OneToOneField(
        "reservations.Reservation", on_delete=models.PROTECT, related_name="hyperbill_invoice"
    )
    merchant_reference = models.CharField(max_length=32, unique=True)
    invoice_no = models.CharField(max_length=64, unique=True, null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="SAR")
    payment_url = models.URLField(max_length=500, blank=True)
    status = models.CharField(max_length=30, default="creating")
    delivery_status = models.CharField(max_length=30, default="not_sent")
    delivery_message_id = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="hyperbill_amount_positive"),
            models.CheckConstraint(condition=Q(currency="SAR"), name="hyperbill_currency_sar"),
        ]

    def __str__(self):
        return f"{self.merchant_reference}: {self.status} (sandbox)"


class HyperBillWebhookSignal(models.Model):
    """Durable empty-body wakeup, not proof that any invoice was paid."""

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"HyperBill sandbox wakeup {self.pk}"
