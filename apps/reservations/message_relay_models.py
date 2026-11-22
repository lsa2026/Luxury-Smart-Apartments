"""Private source dispatch journal. No guest data or capabilities are persisted."""

import uuid

from django.db import models
from django.utils import timezone


class WebsiteMessageRelay(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.CharField(max_length=2)
    financial = models.BooleanField(default=False)
    event_id = models.UUIDField()
    stay_id = models.UUIDField()
    revision = models.CharField(max_length=32)
    message_type = models.CharField(max_length=32)
    job_sha256 = models.CharField(max_length=64)
    source_occurred_at = models.DateTimeField()
    not_before = models.DateTimeField()
    expires_at = models.DateTimeField()
    state = models.CharField(max_length=16, default="pending")
    request_id = models.CharField(max_length=128, blank=True)
    reason = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "financial", "event_id", "message_type"],
                name="website_relay_event_kind_uniq",
            )
        ]
        indexes = [models.Index(fields=["state", "not_before"], name="website_relay_due_idx")]

    def __str__(self):
        return f"{self.scenario}:{self.id}"
