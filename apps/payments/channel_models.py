"""A single private channel-card collection; never a new stay or guest invoice."""

import uuid

from django.conf import settings
from django.db import models


class ChannelCardCollection(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "بانتظار بدء التحصيل"
        CREATED = "created", "جارٍ تجهيز البوابة"
        PENDING = "pending", "بانتظار تأكيد البوابة"
        SUCCEEDED = "succeeded", "تم التحقق من القبض"
        FAILED = "failed", "رفضت البوابة العملية — يلزم مراجعة"
        CANCELLED = "cancelled", "ألغيت العملية — يلزم مراجعة"
        UNKNOWN = "unknown", "نتيجة غير محسومة — لا تعد التحصيل"
        REVIEW = "review", "يلزم مراجعة التحصيل"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hostaway_reservation_id = models.CharField(max_length=32, unique=True, editable=False)
    property = models.ForeignKey("properties.Property", on_delete=models.PROTECT)
    reservation = models.ForeignKey(
        "reservations.Reservation", on_delete=models.PROTECT, null=True, blank=True
    )
    # No remote payload, PAN/CVC, guest identity or card identifier is retained.
    booking_snapshot = models.JSONField(default=dict, editable=False)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    available_on = models.DateField()
    charge_before = models.DateField()
    balance_checked_at = models.DateTimeField()
    payer = models.JSONField(default=dict, editable=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    environment = models.CharField(max_length=10, editable=False)
    entity_fingerprint = models.CharField(max_length=64, blank=True, editable=False)
    approval_reference = models.CharField(max_length=100, blank=True, editable=False)
    merchant_transaction_id = models.CharField(max_length=64, unique=True, editable=False)
    provider_checkout_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, editable=False
    )
    widget_integrity = models.CharField(max_length=255, blank=True, editable=False)
    provider_payment_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, editable=False
    )
    provider_result_code = models.CharField(max_length=100, blank=True, editable=False)
    failure_code = models.CharField(max_length=100, blank=True, editable=False)
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="channel_collection_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(currency="SAR"), name="channel_collection_sar_only"
            ),
            models.CheckConstraint(
                condition=models.Q(charge_before__gt=models.F("available_on")),
                name="channel_collection_charge_window",
            ),
        ]

    def __str__(self):
        return f"Channel collection {self.pk}: {self.status}"
