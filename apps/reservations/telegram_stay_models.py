"""Private dedicated-channel journals. No raw phones, message bodies or capabilities."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class TelegramStayChannel(models.Model):
    scenario = models.CharField(max_length=2)
    stay_id = models.UUIDField()
    hostaway_id = models.CharField(max_length=16)
    cutover_at = models.DateTimeField()
    selected_at = models.DateTimeField(default=timezone.now, editable=False)
    receipt_cursor = models.UUIDField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["scenario", "stay_id"], name="tg_stay_channel_uniq")
        ]

    def __str__(self):
        return f"Telegram channel {self.scenario}:{self.stay_id}"


class TelegramStayNotice(models.Model):
    scenario = models.CharField(max_length=2)
    financial = models.BooleanField(default=False)
    event_id = models.UUIDField()
    stay_id = models.UUIDField()
    revision = models.PositiveBigIntegerField()
    state = models.CharField(max_length=16, default="pending")
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "financial", "event_id"], name="tg_stay_notice_uniq"
            )
        ]

    def __str__(self):
        return f"Telegram event {self.scenario}:{self.event_id}"


class StayMessageJournal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.CharField(max_length=2)
    stay_id = models.UUIDField()
    event_id = models.UUIDField()
    revision = models.PositiveBigIntegerField()
    purpose = models.CharField(max_length=32)
    sequence = models.PositiveSmallIntegerField()
    recipient_kind = models.CharField(max_length=8, default="guest")
    job_key = models.CharField(max_length=220, unique=True)
    reference_id = models.CharField(max_length=220, unique=True)
    identity = models.JSONField()
    # Historical minimal booking metadata excludes names/email/plain phone.
    booking_basis = models.JSONField()
    state = models.CharField(max_length=16, default="planned")
    content_sha256 = models.CharField(max_length=64, blank=True)
    asset_ref = models.CharField(max_length=100, blank=True)
    asset_sha256 = models.CharField(max_length=64, blank=True)
    render_sha256 = models.CharField(max_length=64, blank=True)
    attempt_id = models.UUIDField(null=True)
    provider_message_id = models.CharField(max_length=32, blank=True)
    binding_fingerprint = models.CharField(max_length=64, blank=True)
    accepted_at = models.DateTimeField(null=True)
    not_before = models.DateTimeField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def __str__(self):
        return f"Stay message {self.pk}"


class TelegramStayMessage(StayMessageJournal):
    """Historical direct-guest journal; workgroup receipts never write here."""

    class Meta:
        indexes = [
            models.Index(fields=["scenario", "stay_id", "state"], name="tg_stay_message_state_idx")
        ]


class TelegramWorkgroupHandoff(StayMessageJournal):
    """Copies accepted by the fixed workgroup, never proof of guest delivery."""

    recipient_kind = models.CharField(max_length=16, default="workgroup")

    class Meta:
        indexes = [
            models.Index(fields=["scenario", "stay_id", "state"], name="tg_workgroup_state_idx")
        ]


class TelegramStayActivation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.CharField(max_length=2)
    stay_id = models.UUIDField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    state = models.CharField(max_length=16, default="processing")
    evidence_ref = models.CharField(max_length=100)
    phone_digest = models.CharField(max_length=64)
    proof_id = models.UUIDField(default=uuid.uuid4, editable=False)
    approve_authorized_at = models.DateTimeField(null=True)
    issue_authorized_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["scenario", "stay_id"], name="tg_stay_activation_uniq")
        ]

    def __str__(self):
        return f"Telegram stay proof {self.pk}"


class TelegramWebsiteHandoff(models.Model):
    """Standalone website copies. No message body, phone, link or capability."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_kind = models.CharField(max_length=32)
    source_id = models.CharField(max_length=180)
    source_revision = models.CharField(max_length=64)
    job_key = models.CharField(max_length=220, unique=True)
    reference_id = models.CharField(max_length=100, unique=True)
    phone_hash = models.CharField(max_length=64)
    content_sha256 = models.CharField(max_length=64)
    asset_sha256 = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default="planned")
    attempt_id = models.UUIDField(null=True)
    provider_message_id = models.CharField(max_length=32, blank=True)
    provider_message_ids = models.JSONField(default=list)
    accepted_at = models.DateTimeField(null=True)
    not_before = models.DateTimeField()
    expires_at = models.DateTimeField()
    probes = models.PositiveSmallIntegerField(default=0)
    next_probe_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Website workgroup copy {self.pk}"
