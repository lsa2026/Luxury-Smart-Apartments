"""Internal morning stays; never represented as overnight Hostaway bookings."""

import uuid

from django.conf import settings
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone


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
    guest_language = models.CharField(
        max_length=2,
        default="ar",
        choices=[("ar", "العربية"), ("en", "English"), ("fr", "Français")],
    )
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
    communication_revision = models.PositiveBigIntegerField(default=0, editable=False)
    communication_fingerprint = models.CharField(max_length=64, blank=True, editable=False)

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

    def save(self, *args, **kwargs):
        # The outbox is committed with the business change, including payment verification.
        with transaction.atomic():
            if not self._state.adding:
                previous = (
                    type(self)
                    .objects.select_for_update()
                    .only("communication_revision", "communication_fingerprint")
                    .get(pk=self.pk)
                )
                self.communication_revision = previous.communication_revision
                self.communication_fingerprint = previous.communication_fingerprint
            super().save(*args, **kwargs)
            if getattr(settings, "MORNING_N8N_ENABLED", False):
                from .morning_communications import capture_event

                capture_event(self.pk)
                self.refresh_from_db(fields=["communication_revision", "communication_fingerprint"])

    def latest_communication_event(self):
        return self.communication_events.order_by("-revision").first()


class MorningCommunicationEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stay = models.ForeignKey(
        MorningStay, on_delete=models.PROTECT, related_name="communication_events"
    )
    revision = models.PositiveBigIntegerField()
    payload = models.JSONField(default=dict, editable=False)
    state = models.CharField(max_length=12, default="pending", editable=False)
    attempts = models.PositiveIntegerField(default=0, editable=False)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True, editable=False)
    accepted_at = models.DateTimeField(null=True, editable=False)
    last_error = models.CharField(max_length=80, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["stay", "revision"], name="morning_event_revision_unique"
            )
        ]
        indexes = [models.Index(fields=["state", "next_attempt_at"])]

    def __str__(self):
        return f"Morning communication {self.stay_id} revision {self.revision}: {self.state}"
