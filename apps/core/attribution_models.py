"""Private attribution evidence. No guest fields or booking capabilities."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class AttributionHandoff(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference_hash = models.CharField(max_length=64, unique=True, editable=False)
    journey_hash = models.CharField(max_length=64, db_index=True, editable=False)
    snapshot = models.JSONField(default=dict, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return "Advertising handoff"


class BookingAttribution(models.Model):
    """One immutable snapshot for one explicit booking anchor."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    quote = models.OneToOneField(
        "reservations.BookingQuote",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="advertising_attribution",
    )
    morning_stay = models.OneToOneField(
        "reservations.MorningStay",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="advertising_attribution",
    )
    day_use_stay = models.OneToOneField(
        "reservations.DayUseStay",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="advertising_attribution",
    )
    handoff = models.OneToOneField(
        AttributionHandoff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="binding",
    )
    journey_hash = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    snapshot = models.JSONField(default=dict, editable=False)
    linkage = models.CharField(max_length=20)
    closure_method = models.CharField(max_length=20)
    staff_source = models.CharField(max_length=30, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(quote__isnull=False, morning_stay__isnull=True, day_use_stay__isnull=True)
                    | Q(quote__isnull=True, morning_stay__isnull=False, day_use_stay__isnull=True)
                    | Q(quote__isnull=True, morning_stay__isnull=True, day_use_stay__isnull=False)
                ),
                name="attribution_one_booking_anchor",
            ),
        ]

    def __str__(self):
        return f"Booking source — {self.linkage}"
