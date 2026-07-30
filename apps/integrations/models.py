import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class IntegrationSyncRun(models.Model):
    class SyncType(models.TextChoices):
        HOSTAWAY_PROPERTIES = "hostaway_properties", "Hostaway properties"
        HOSTAWAY_REVIEWS = "hostaway_reviews", "Hostaway reviews"

    class Status(models.TextChoices):
        RUNNING = "running", "قيد التشغيل"
        SUCCEEDED = "succeeded", "نجحت"
        PARTIALLY_SUCCEEDED = "partially_succeeded", "نجحت جزئيًا"
        FAILED = "failed", "فشلت"

    sync_type = models.CharField(max_length=40, choices=SyncType.choices)
    status = models.CharField(max_length=30, choices=Status.choices)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    fetched_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="integration_sync_runs",
    )
    dry_run = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["sync_type", "-started_at"]),
            models.Index(fields=["status", "-started_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sync_type"],
                condition=Q(status="running"),
                name="one_running_sync_per_type",
            ),
        ]
        verbose_name = "تشغيل مزامنة"
        verbose_name_plural = "سجل المزامنة"

    def __str__(self) -> str:
        return f"{self.get_sync_type_display()} — {self.get_status_display()}"


class HostawayWebhookEvent(models.Model):
    """Sanitized, deduplicated Unified Webhook event queued for later processing."""

    class Status(models.TextChoices):
        RECEIVED = "received", "مستلم"
        PROCESSING = "processing", "قيد المعالجة"
        PROCESSED = "processed", "معالج"
        IGNORED = "ignored", "متجاهل"
        RETRYABLE = "retryable", "قابل لإعادة المحاولة"
        FAILED = "failed", "فشل دائم"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_event_id = models.CharField(max_length=255, null=True, blank=True)
    event_type = models.CharField(max_length=100)
    hostaway_object_id = models.CharField(max_length=255, null=True, blank=True)
    hostaway_reservation_id = models.PositiveBigIntegerField(null=True, blank=True)
    deduplication_key = models.CharField(max_length=64, unique=True, editable=False)
    body_hash = models.CharField(max_length=64, editable=False)
    sanitized_payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RECEIVED,
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    received_at = models.DateTimeField(default=timezone.now)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["status", "next_retry_at"]),
            models.Index(fields=["event_type", "-received_at"]),
            models.Index(fields=["hostaway_reservation_id"]),
            models.Index(fields=["received_at"]),
        ]
        verbose_name = "حدث Hostaway Webhook"
        verbose_name_plural = "أحداث Hostaway Webhook"

    def __str__(self) -> str:
        return f"{self.event_type} — {self.status}"
