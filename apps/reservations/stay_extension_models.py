"""Explicit links between independent bookings; never modifies an OTA original."""

import uuid

from django.conf import settings
from django.db import models


class StayExtensionChain(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey("properties.Property", on_delete=models.PROTECT)
    root_hostaway_reservation_id = models.PositiveBigIntegerField(unique=True)
    # Dates, listing, channel and status only. Guest data lives on the new draft.
    root_snapshot = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=0)
    fingerprint = models.CharField(max_length=64, blank=True)
    review_required = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return str(self.pk)


class StayExtension(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chain = models.ForeignKey(
        StayExtensionChain, on_delete=models.PROTECT, related_name="extensions"
    )
    previous_hostaway_reservation_id = models.PositiveBigIntegerField()
    previous_snapshot = models.JSONField(default=dict)
    draft = models.OneToOneField(
        "reservations.ManualBookingDraft", on_delete=models.PROTECT, related_name="stay_extension"
    )
    reservation = models.OneToOneField(
        "reservations.Reservation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stay_extension",
    )
    state = models.CharField(max_length=24, default="draft")
    activation_generation = models.PositiveIntegerField(default=0)
    ownership_acknowledged = models.BooleanField(default=False)
    last_error = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["previous_hostaway_reservation_id"],
                condition=~models.Q(state="cancelled"),
                name="extension_open_parent_unique",
            )
        ]

    def __str__(self):
        return str(self.pk)


class StayExtensionEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    extension = models.ForeignKey(StayExtension, on_delete=models.PROTECT, related_name="events")
    revision = models.PositiveIntegerField()
    kind = models.CharField(max_length=48)
    state = models.CharField(max_length=16, default="pending")
    attempts = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["extension", "revision"], name="extension_event_revision_unique"
            )
        ]

    def __str__(self):
        return str(self.pk)
