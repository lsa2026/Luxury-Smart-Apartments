"""Private administration records, never guest credentials or operational stays."""

import uuid

from django.db import models


class GuestDirectorySnapshot(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    data = models.JSONField(default=dict)
    completed_at = models.DateTimeField()

    def __str__(self):
        return "Verified guest directory"


class AdminGuestFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity_key = models.CharField(max_length=64, unique=True)
    email_key = models.CharField(max_length=64, blank=True)
    phone_key = models.CharField(max_length=64, blank=True)
    classification = models.CharField(max_length=16, default="unknown")
    basis = models.CharField(max_length=250, blank=True)
    history = models.JSONField(default=list)
    checked_at = models.DateTimeField(null=True)

    def __str__(self):
        return f"Guest file {self.pk}"


class AdminGuestAnchor(models.Model):
    hostaway_reservation_id = models.PositiveBigIntegerField(primary_key=True)
    guest_file = models.ForeignKey(AdminGuestFile, on_delete=models.CASCADE, related_name="anchors")

    def __str__(self):
        return f"Guest booking anchor {self.pk}"
