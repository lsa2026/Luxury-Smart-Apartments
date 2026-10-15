"""Provider-neutral payment records; none are created automatically."""

import uuid

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy


class ReservationPaymentRequest(models.Model):
    """A revocable payment-only capability for an already-created manual stay."""

    class Status(models.TextChoices):
        OPEN = "open", _("Awaiting payment")
        PAID = "paid", _("Paid")
        REVOKED = "revoked", _("Cancelled")
        REVIEW = "review", _("Needs review")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.ForeignKey(
        "reservations.Reservation", on_delete=models.PROTECT, related_name="payment_requests"
    )
    amount_sar = models.DecimalField(max_digits=14, decimal_places=2)
    source_amount = models.DecimalField(max_digits=14, decimal_places=4)
    source_currency = models.CharField(max_length=3)
    fingerprint = models.CharField(max_length=64, editable=False)
    language = models.CharField(max_length=2, default="ar")
    environment = models.CharField(max_length=10, default="test", editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    legal_acceptance = models.JSONField(default=dict, editable=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    review_code = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["reservation"],
                condition=Q(status="open"),
                name="one_open_reservation_payment_request",
            ),
            models.CheckConstraint(
                condition=Q(amount_sar__gt=0) & Q(source_amount__gt=0),
                name="reservation_payment_request_positive",
            ),
        ]

    def __str__(self):
        return f"Payment request {self.pk}: {self.status}"


class PaymentRequestDelivery(models.Model):
    """Delivery evidence only; never retain a bearer link or message body."""

    request = models.ForeignKey(
        ReservationPaymentRequest, on_delete=models.PROTECT, related_name="deliveries"
    )
    kind = models.CharField(max_length=12, choices=[("link", "Link"), ("receipt", "Receipt")])
    sequence = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, default="queued")
    provider_message_id = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["request", "kind", "sequence"], name="unique_payment_request_delivery"
            )
        ]

    def __str__(self):
        return f"Payment delivery {self.pk}: {self.status}"


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
    payment_request = models.ForeignKey(
        ReservationPaymentRequest,
        on_delete=models.PROTECT,
        related_name="attempts",
        null=True,
        blank=True,
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
    payment_brand = models.CharField(max_length=30, blank=True, editable=False)
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


class HostawayFinancialEntry(models.Model):
    """One durable receipt request per verified payment; no historical backfill."""

    payment_attempt = models.OneToOneField(
        PaymentAttempt, on_delete=models.PROTECT, related_name="hostaway_receipt"
    )
    status = models.CharField(
        max_length=20,
        default="pending",
        choices=[
            ("pending", "Pending"),
            ("posting", "Posting"),
            ("succeeded", "Recorded"),
            ("unknown", "Unknown outcome"),
            ("review", "Needs accounting review"),
        ],
    )
    remote_charge_id = models.PositiveBigIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Hostaway receipt {self.pk}: {self.status}"
