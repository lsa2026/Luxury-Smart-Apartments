from django.conf import settings
from django.db import models
from django.db.models import Q


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
