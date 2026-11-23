"""Explicit server-operator notification requests. No refund/booking write or API."""

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models.functions import MD5
from django.utils import timezone

from apps.notifications.models import AuditLog

from . import telegram_website_workgroup as transport
from . import telegram_workgroup as group
from .models import RefundObligation, Reservation
from .telegram_authority import Denied
from .telegram_stay_models import TelegramWebsiteHandoff

ACTION = "refund.workgroup_copy_requested"
PREFIX = "SITE01_APPROVED_REFUND_COPY "
SAFE_REASONS = {
    "workgroup_paused",
    "booking_lookup_not_unique",
    "booking_identity_or_dates_mismatch",
    "confirmed_refund_not_unique",
    "different_prior_approval",
}
FIELDS = {
    "request_id",
    "reference_md5",
    "reference_sha256",
    "check_in",
    "old_check_out",
    "check_out",
    "old_total",
    "currency",
}


def emit(request_id, decision, **evidence):
    print(
        PREFIX
        + json.dumps({"request_id": request_id, "decision": decision, **evidence}, sort_keys=True),
        flush=True,
    )


def project(refund, basis):
    """A new copy request never changes the original transfer's financial time."""
    row = (
        AuditLog.objects.filter(
            action=ACTION, object_type="RefundObligation", object_reference=str(refund.pk)
        )
        .order_by("created_at")
        .first()
    )
    if not row:
        return refund.transferred_at
    metadata = row.metadata
    if (
        metadata.get("contract") != "site01_owner_refund_copy_v1"
        or metadata.get("financial_basis") != transport.digest(basis)
        or not isinstance(metadata.get("request_id"), str)
        or not transport.admitted(row.created_at)
        or row.created_at < refund.transferred_at
        or row.created_at > timezone.now()
    ):
        raise Denied
    basis.update(
        owner_copy_request=metadata["request_id"],
        original_transfer_at=refund.transferred_at.isoformat(),
    )
    return row.created_at


def run_request(value):
    """Invoked only by the existing predeploy migrate after an explicit env arm."""
    from .telegram_booking_events import queue_event, source

    if not group.enabled():
        raise Denied("workgroup_paused")
    p = json.loads(value)
    if not isinstance(p, dict) or set(p) != FIELDS:
        raise ValueError("invalid_request")
    request_id = str(uuid.UUID(p["request_id"]))
    if request_id != p["request_id"]:
        raise ValueError("invalid_request_id")
    dates = [date.fromisoformat(p[k]) for k in ("check_in", "old_check_out", "check_out")]
    if not dates[0] < dates[2] < dates[1] or p["currency"] != "SAR":
        raise ValueError("invalid_scope")
    with transaction.atomic():
        # MD5 is only a narrow lookup, not authority. Verify SHA256 and full scope
        # before any notification write. No guest payload is returned or logged.
        bookings = list(
            Reservation.objects.annotate(lookup=MD5("public_reference"))
            .filter(lookup=p["reference_md5"])
            .select_for_update()[:2]
        )
        if len(bookings) != 1:
            raise Denied("booking_lookup_not_unique")
        booking = bookings[0]
        if (
            hashlib.sha256(booking.public_reference.encode()).hexdigest() != p["reference_sha256"]
            or booking.check_in != dates[0]
            or booking.check_out != dates[2]
            or booking.currency != p["currency"]
            or booking.is_test
        ):
            raise Denied("booking_identity_or_dates_mismatch")
        refunds = list(
            booking.refund_obligations.select_for_update(of=("self",)).filter(
                status=RefundObligation.Status.TRANSFERRED,
                reason=RefundObligation.Reason.MODIFICATION_DECREASE,
                modification_request__old_check_in=dates[0],
                modification_request__old_check_out=dates[1],
                modification_request__new_check_out=dates[2],
                modification_request__old_total=Decimal(p["old_total"]),
            )[:2]
        )
        if len(refunds) != 1:
            raise Denied("confirmed_refund_not_unique")
        refund = refunds[0]
        source_id = "refund:" + str(refund.pk)
        job_key = "site01wg:booking_event:" + transport.sha(source_id)
        existing = TelegramWebsiteHandoff.objects.filter(job_key=job_key).first()
        if existing:
            result = {"decision": "existing_" + existing.state, "request_id": request_id}
            if existing.state == "handed_off":
                result["provider_message_ids"] = existing.provider_message_ids
            transaction.on_commit(lambda: emit(**result), robust=True)
            return result
        # The same real-model proof used by automatic confirmation runs first.
        raw = source(source_id)
        basis = raw[5]
        prior = AuditLog.objects.filter(
            action=ACTION, object_type="RefundObligation", object_reference=str(refund.pk)
        ).first()
        if prior:
            if prior.metadata.get("request_id") != request_id:
                raise Denied("different_prior_approval")
        else:
            AuditLog.objects.create(
                action=ACTION,
                object_type="RefundObligation",
                object_reference=str(refund.pk),
                summary="Owner-authorized workgroup confirmation copy; no financial action",
                metadata={
                    "contract": "site01_owner_refund_copy_v1",
                    "request_id": request_id,
                    "reference_sha256": p["reference_sha256"],
                    "financial_basis": transport.digest(basis),
                },
            )
        queue_event("refund", refund.pk)
        journal = TelegramWebsiteHandoff.objects.get(job_key=job_key)
        result = {"decision": "queued", "request_id": request_id, "job_id": str(journal.pk)}
        transaction.on_commit(lambda: emit(**result), robust=True)
        return result


def after_migrate(sender, **kwargs):
    value = getattr(settings, "TELEGRAM_APPROVED_REFUND_COPY_REQUEST", "")
    if sender.label != "reservations" or not value:
        return
    request_id = "invalid"
    try:
        request_id = json.loads(value).get("request_id", "invalid")
        run_request(value)
    except Exception as exc:
        # Private provider/source exceptions must never enter deployment logs.
        try:
            safe_id = str(uuid.UUID(str(request_id)))
        except (ValueError, TypeError, AttributeError):
            safe_id = "invalid"
        reason = (
            exc.args[0]
            if exc.args and isinstance(exc.args[0], str) and exc.args[0] in SAFE_REASONS
            else "source_proof_requires_review"
        )
        emit(safe_id, "held_source_or_existing_attempt_requires_review", reason=reason)


def completion(row):
    """Evidence only after the database commit, never before accepted-part proof."""
    if row.source_kind != "booking_event" or row.state != "handed_off":
        return
    kind, refund_id = row.source_id.split(":", 1)
    if kind != "refund":
        return
    approval = AuditLog.objects.filter(
        action=ACTION, object_type="RefundObligation", object_reference=refund_id
    ).first()
    if approval:
        request_id, ids = approval.metadata["request_id"], list(row.provider_message_ids)
        transaction.on_commit(
            lambda: emit(
                request_id,
                "handed_off",
                provider_message_ids=ids,
                guest_accepted=False,
                delivered=False,
                read=False,
            ),
            robust=True,
        )
