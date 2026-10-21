"""Internal morning stays; never represented as overnight Hostaway bookings."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q


class MorningStay(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "بانتظار الدفع"
        PROCESSING = "processing", "جارٍ التحقق من الدفع"
        PAID = "paid", "مدفوع"
        EXPIRED = "expired", "انتهت مهلة الدفع"
        CANCELLED = "cancelled", "ملغى"
        REVIEW = "review", "يحتاج مراجعة"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.UUIDField(unique=True, editable=False)
    property = models.ForeignKey("properties.Property", on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    time_zone = models.CharField(max_length=40, default="Asia/Riyadh", editable=False)
    guest_name = models.CharField(max_length=200)
    guest_phone = models.CharField(max_length=32)
    guest_email = models.EmailField()
    guests = models.PositiveSmallIntegerField(default=1)
    amount_sar = models.DecimalField(max_digits=14, decimal_places=2)
    environment = models.CharField(max_length=10, editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    expires_at = models.DateTimeField()
    availability_checked_at = models.DateTimeField()
    availability_evidence = models.JSONField(default=dict, editable=False)
    legal_acceptance = models.JSONField(default=dict, blank=True, editable=False)
    paid_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["property", "starts_at", "ends_at", "status"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")), name="morning_positive_window"
            ),
            models.CheckConstraint(condition=Q(amount_sar__gt=0), name="morning_positive_amount"),
        ]

    def __str__(self):
        return f"AM-{str(self.pk)[:8]} — {self.get_status_display()}"
