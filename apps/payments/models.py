"""Provider-neutral payment records; none are created automatically."""

import uuid

from django.db import models
from django.db.models import Q


class PaymentAttempt(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "منشأة"
        PENDING = "pending", "قيد المعالجة"
        SUCCEEDED = "succeeded", "ناجحة"
        FAILED = "failed", "فاشلة"
        CANCELLED = "cancelled", "ملغاة"
        EXPIRED = "expired", "منتهية"
        REFUNDED = "refunded", "مستردة"
        PARTIALLY_REFUNDED = "partially_refunded", "مستردة جزئيًا"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking_intent = models.ForeignKey(
        "reservations.BookingIntent",
        on_delete=models.PROTECT,
        related_name="payment_attempts",
    )
    provider = models.CharField(max_length=50)
    provider_reference = models.CharField(max_length=255, null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=4)
    currency = models.CharField(max_length=3)
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.CREATED,
    )
    idempotency_key = models.CharField(max_length=64, unique=True)
    failure_code = models.CharField(max_length=100, blank=True)
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
        verbose_name = "محاولة دفع"
        verbose_name_plural = "محاولات الدفع"

    def __str__(self) -> str:
        return f"{self.provider} — {self.amount} {self.currency}"
