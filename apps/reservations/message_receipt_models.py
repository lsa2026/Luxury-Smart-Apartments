"""Immutable pre-attempt expectation and independently fetched central receipts."""

import uuid

from django.db import models
from django.utils import timezone


class WebsiteMessageExpectation(models.Model):
    request_id = models.CharField(primary_key=True, max_length=70)
    relay = models.OneToOneField("reservations.WebsiteMessageRelay", on_delete=models.PROTECT)
    expectation_ref = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    prepare_ref = models.CharField(max_length=64, unique=True)
    identity = models.JSONField()
    source_binding = models.JSONField(default=dict, db_default={}, editable=False)
    render_policy_ref = models.CharField(max_length=128)
    prepared_at_epoch = models.BigIntegerField()
    registered_at = models.DateTimeField(default=timezone.now, editable=False)
    state = models.CharField(max_length=16, default="prepared")
    attempts_claimed = models.PositiveSmallIntegerField(default=0)
    provider_message_id = models.CharField(max_length=512, blank=True)
    wamid = models.CharField(max_length=512, blank=True)
    receipt_evidence_ref = models.CharField(max_length=512, blank=True)
    observed_at_epoch = models.BigIntegerField(null=True)

    def __str__(self):
        return self.request_id
