"""Private review scheduling/claim metadata, never guest bodies or capabilities."""

import uuid

from django.db import models
from django.utils import timezone


class WebsiteReviewCampaign(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.CharField(max_length=2)
    stay_id = models.UUIDField()
    origin_event_id = models.UUIDField()
    source_occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "stay_id"], name="website_review_stay_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(scenario__in=["07", "08"]), name="website_review_scenario"
            ),
        ]

    def __str__(self):
        return f"Review campaign {self.id}"


class WebsiteReviewSlot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(
        WebsiteReviewCampaign, on_delete=models.PROTECT, related_name="slots"
    )
    step = models.PositiveSmallIntegerField()
    job_sha256 = models.CharField(max_length=64, unique=True, editable=False)
    not_before = models.DateTimeField()
    expires_at = models.DateTimeField()
    basis = models.JSONField(default=dict, editable=False)
    expectation = models.OneToOneField(
        "reservations.WebsiteMessageExpectation",
        on_delete=models.PROTECT,
        null=True,
        editable=False,
        related_name="review_slot",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["campaign", "step"], name="website_review_step_uniq"),
            models.CheckConstraint(
                condition=models.Q(step__gte=1, step__lte=3), name="website_review_step_range"
            ),
        ]

    def __str__(self):
        return f"Review slot {self.id}"
