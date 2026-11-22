"""Durable issuance guard. Never stores invitation URLs, phone numbers or tokens."""

import uuid

from django.conf import settings
from django.db import models


class TelegramActivation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.OneToOneField(
        "reservations.Reservation", on_delete=models.PROTECT, related_name="telegram_activation"
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    state = models.CharField(max_length=16, default="processing")
    proof_kind = models.CharField(max_length=20)
    evidence_ref = models.CharField(max_length=100, blank=True)
    phone_digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"TelegramActivation {self.pk}"
