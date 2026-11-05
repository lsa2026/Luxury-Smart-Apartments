"""SMA's existing bearer identity may claim/complete drafts, never send email."""

import hashlib
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.accounts.access import is_operations_owner
from apps.reservations.sama_booking_api import authenticated, payload, response

from .models import ContactAIDraft


@authenticated("POST")
def claim(request):
    if payload(request, []) is None:
        return response("invalid_request", 400)
    with transaction.atomic():
        ContactAIDraft.objects.filter(
            status="processing", started_at__lt=timezone.now() - timedelta(minutes=5)
        ).update(status="failed")
        draft = (
            ContactAIDraft.objects.select_for_update(skip_locked=True)
            .filter(status="pending")
            .select_related("contact", "requested_by")
            .order_by("created_at")
            .first()
        )
        if not draft:
            return response("empty")
        contact = draft.contact
        digest = hashlib.sha256((contact.subject + "\n" + contact.message).encode()).hexdigest()
        if not is_operations_owner(draft.requested_by) or digest != draft.source_hash:
            draft.status = "failed"
            draft.save(update_fields=["status"])
            return response("empty")
        draft.status = "processing"
        draft.lease = uuid.uuid4()
        draft.started_at = timezone.now()
        draft.save(update_fields=["status", "lease", "started_at"])
        return response(
            "claimed",
            draft={
                "id": str(draft.pk),
                "lease": str(draft.lease),
                "subject": contact.subject,
                "message": contact.message,
                "language": draft.language,
                "instructions": draft.instructions,
            },
        )


@authenticated("POST")
def complete(request):
    data = payload(request, ["id", "lease", "body", "status"])
    if (
        not data
        or data["status"] not in {"ready", "failed"}
        or not isinstance(data["body"], str)
        or len(data["body"]) > 2000
        or (data["status"] == "ready" and not data["body"].strip())
    ):
        return response("invalid_request", 400)
    with transaction.atomic():
        draft = (
            ContactAIDraft.objects.select_for_update()
            .filter(pk=data["id"], lease=data["lease"])
            .select_related("contact", "requested_by")
            .first()
        )
        if not draft or draft.status != "processing":
            return response("stale", 409)
        digest = hashlib.sha256(
            (draft.contact.subject + "\n" + draft.contact.message).encode()
        ).hexdigest()
        if digest != draft.source_hash or not is_operations_owner(draft.requested_by):
            draft.status = "failed"
            draft.save(update_fields=["status"])
            return response("stale", 409)
        draft.status = data["status"]
        draft.body = data["body"].strip() if data["status"] == "ready" else ""
        draft.save(update_fields=["status", "body"])
    return response("completed")
