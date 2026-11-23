"""Private frozen invoice source; sender19 alone uploads/posts to YCloud."""

import base64
import hashlib
import json
import re
import uuid
from datetime import UTC, timedelta

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.notifications.models import GuestInvoiceDelivery, SamaInvoiceDispatch
from apps.notifications.services.invoice_delivery import MESSAGES
from apps.notifications.services.invoice_email import invoice_filename

CONTRACT = "site01_sma_invoice_document_v1"
ORIGIN = "https://sma-ycloud-test.onrender.com"
MAX_PDF = 7_000_000


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def iso(value):
    return value.astimezone(UTC).isoformat()


def fingerprint(row):
    return sha(
        json.dumps(
            [
                CONTRACT,
                str(row.request_id),
                str(row.delivery_id),
                str(row.delivery.invoice_id),
                row.pdf_sha256,
                row.recipient_sha256,
                row.language,
                row.caption_sha256,
                iso(row.not_before),
                iso(row.expires_at),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def pdf(delivery):
    return bytes(
        delivery.presentation.pdf_content
        if delivery.presentation_id
        else delivery.invoice.pdf_content
    )


def projection(row):
    return {
        "request_id": str(row.request_id),
        "delivery_id": str(row.delivery_id),
        "source_fingerprint": row.source_fingerprint,
        "not_before": iso(row.not_before),
        "expires_at": iso(row.expires_at),
        "invoice_id": str(row.delivery.invoice_id),
        "recipient_sha256": row.recipient_sha256,
        "language": row.language,
        "pdf_sha256": row.pdf_sha256,
        "pdf_bytes": row.pdf_bytes,
        "pdf_mime": "application/pdf",
        "pdf_filename": row.pdf_filename,
        "caption": row.caption,
        "caption_sha256": row.caption_sha256,
    }


def queue(*, invoice, recipient, actor, request_id):
    if not settings.SAMA_INVOICE_YCLOUD_ENABLED or not invoice.issued_at or not invoice.pdf_content:
        raise ValidationError("SMA invoice source unavailable")
    with transaction.atomic():
        delivery, created = GuestInvoiceDelivery.objects.get_or_create(
            invoice=invoice,
            channel="whatsapp",
            recipient=recipient,
            defaults={"requested_by": actor, "presentation": invoice.presentations.first()},
        )
        if not created:
            binding = SamaInvoiceDispatch.objects.filter(
                delivery=delivery, request_id=request_id
            ).first()
            return delivery, binding  # No adoption, retry, transport switch or second PDF.
        content = pdf(delivery)
        if not content.startswith(b"%PDF-") or not 0 < len(content) <= MAX_PDF:
            raise ValidationError("Invoice document unavailable")
        now = timezone.now()
        row = SamaInvoiceDispatch(
            delivery=delivery,
            request_id=request_id,
            pdf_sha256=sha(content),
            pdf_bytes=len(content),
            pdf_filename=invoice_filename(invoice),
            recipient_sha256=sha(recipient),
            language=invoice.language,
            caption=MESSAGES[invoice.language][1],
            caption_sha256=sha(MESSAGES[invoice.language][1]),
            not_before=now + timedelta(seconds=15),
            expires_at=now + timedelta(minutes=15),
        )
        row.source_fingerprint = fingerprint(row)
        row.save(force_insert=True)
        return delivery, row


def bound(phone, request_id, delivery_id=None, *, lock=False):
    query = SamaInvoiceDispatch.objects.select_related(
        "delivery__invoice", "delivery__presentation"
    )
    if lock:
        query = query.select_for_update(of=("self",))
    row = query.get(request_id=uuid.UUID(request_id))
    delivery = row.delivery
    if (
        (delivery_id is not None and str(delivery.pk) != delivery_id)
        or delivery.channel != "whatsapp"
        or delivery.requested_by.username != "sama-booking-agent"
        or delivery.recipient != phone
        or delivery.invoice.snapshot.get("guest_phone") != phone
        or sha(phone) != row.recipient_sha256
        or fingerprint(row) != row.source_fingerprint
    ):
        raise ValidationError("Invoice scope mismatch")
    return row


def document(row):
    delivery = row.delivery
    invoice = delivery.invoice
    content = pdf(delivery)
    if (
        not settings.SAMA_INVOICE_YCLOUD_ENABLED
        or delivery.status != "queued"
        or not row.not_before <= timezone.now() < row.expires_at
        or not invoice.issued_at
        or invoice.language != row.language
        or len(content) != row.pdf_bytes
        or sha(content) != row.pdf_sha256
        or sha(row.caption) != row.caption_sha256
        or not content.startswith(b"%PDF-")
    ):
        raise ValidationError("Invoice document unavailable")
    return {
        "contract": CONTRACT,
        **projection(row),
        "invoice_id": str(invoice.pk),
        "recipient_e164": delivery.recipient,
        "recipient_sha256": row.recipient_sha256,
        "language": row.language,
        "pdf_sha256": row.pdf_sha256,
        "pdf_bytes": row.pdf_bytes,
        "pdf_mime": "application/pdf",
        "pdf_filename": row.pdf_filename,
        "pdf_base64": base64.b64encode(content).decode("ascii"),
        "caption": row.caption,
        "caption_sha256": row.caption_sha256,
    }


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def fetch_receipt(request_id, capability):
    if not isinstance(capability, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,2048}", capability):
        raise ValidationError("Receipt unavailable")
    url = f"{ORIGIN}/v1/sama/invoices/receipts/{uuid.UUID(request_id)}/"
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", url, headers={"X-LSA-Receipt-Capability": capability}) as result:
            if result.status_code != 200:
                raise ValidationError("Receipt unavailable")
            body = bytearray()
            for chunk in result.iter_bytes():
                body.extend(chunk)
                if len(body) > 32768:
                    raise ValidationError("Receipt unavailable")
    return json.loads(body, object_pairs_hook=unique)


def checked_time(value):
    stamp = parse_datetime(value) if isinstance(value, str) else None
    if not stamp or not timezone.is_aware(stamp):
        raise ValidationError("Receipt scope mismatch")
    return stamp


def import_receipt(row, value):
    expected = {
        "contract": "sma19_invoice_receipt_v1",
        "transport": "ycloud",
        "request_id": str(row.request_id),
        "delivery_id": str(row.delivery_id),
        "invoice_id": str(row.delivery.invoice_id),
        "source_fingerprint": row.source_fingerprint,
        "recipient_sha256": row.recipient_sha256,
        "pdf_sha256": row.pdf_sha256,
        "caption_sha256": row.caption_sha256,
        "language": row.language,
    }
    if not isinstance(value, dict) or any(
        type(value.get(k)) is not type(v) or value[k] != v for k, v in expected.items()
    ):
        raise ValidationError("Receipt scope mismatch")
    state, outcome = value.get("state"), value.get("outcome")
    attempts = value.get("attempts_claimed")
    if type(attempts) is not int or attempts not in {0, 1}:
        raise ValidationError("Receipt scope mismatch")
    accepted = state in {"accepted", "sent", "delivered", "read"} and outcome == "accepted"
    if not accepted and (state, outcome) not in {
        ("rejected", "rejected"),
        ("unknown", "unknown"),
        ("cancelled", "rejected"),
        ("expired", "rejected"),
    }:
        raise ValidationError("Receipt scope mismatch")
    if not accepted and value.get("accepted_at") is not None:
        raise ValidationError("Receipt scope mismatch")
    if not isinstance(value.get("outbox_id"), str):
        raise ValidationError("Receipt scope mismatch")
    outbox = uuid.UUID(value["outbox_id"])
    if str(outbox) != value["outbox_id"]:
        raise ValidationError("Receipt scope mismatch")
    observed = checked_time(value.get("observed_at"))
    accepted_at = checked_time(value.get("accepted_at")) if accepted else None
    if not row.created_at <= observed <= timezone.now() + timedelta(seconds=5):
        raise ValidationError("Receipt time mismatch")
    if attempts == 0:
        if (
            state not in {"cancelled", "expired", "unknown"}
            or value.get("claimed_at") is not None
            or value.get("provider_message_id") is not None
            or value.get("wamid") is not None
            or value.get("accepted_at") is not None
            or row.delivery.attempt_count
        ):
            raise ValidationError("Preclaim receipt mismatch")
    else:
        claimed = checked_time(value.get("claimed_at"))
        if (
            state in {"cancelled", "expired"}
            or not row.not_before <= claimed < row.expires_at
            or not claimed <= observed
            or (accepted and not claimed <= accepted_at <= observed)
        ):
            raise ValidationError("Receipt time mismatch")
    for field in ("provider_message_id", "wamid", "business_sender", "waba_id", "phone_id"):
        item = value.get(field) or ""
        if not isinstance(item, str) or len(item) > 255:
            raise ValidationError("Receipt scope mismatch")
        previous = getattr(row, field)
        if previous and previous != item:
            raise ValidationError("Receipt identity changed")
    if (
        (row.outbox_id and row.outbox_id != outbox)
        or (row.accepted_at and accepted_at and row.accepted_at != accepted_at)
        or (accepted and not value.get("provider_message_id"))
        or not re.fullmatch(r"\+[1-9][0-9]{6,14}", value.get("business_sender", ""))
        or not re.fullmatch(r"[1-9][0-9]{1,31}", value.get("waba_id", ""))
        or not re.fullmatch(r"[1-9][0-9]{1,31}", value.get("phone_id", ""))
        or not isinstance(value.get("receipt_evidence_ref"), str)
        or not 1 <= len(value["receipt_evidence_ref"]) <= 255
    ):
        raise ValidationError("Receipt scope mismatch")
    rank = {
        "unknown": 0,
        "rejected": 0,
        "cancelled": 0,
        "expired": 0,
        "accepted": 1,
        "sent": 2,
        "delivered": 3,
        "read": 4,
    }
    if row.observed_at and observed < row.observed_at:
        return
    if row.provider_state and rank[state] < rank[row.provider_state]:
        return  # The stored equal-identity evidence is stronger; never downgrade.
    row.outbox_id, row.provider_state, row.observed_at = outbox, state, observed
    row.accepted_at = row.accepted_at or accepted_at
    for field in ("provider_message_id", "wamid", "business_sender", "waba_id", "phone_id"):
        setattr(row, field, value.get(field) or "")
    row.receipt_evidence_ref = value["receipt_evidence_ref"]
    row.proof_sha256 = sha(json.dumps(value, sort_keys=True, separators=(",", ":")))
    row.save()
    delivery = row.delivery
    delivery.status = "accepted" if accepted else "review" if state == "unknown" else "failed"
    delivery.accepted_at = delivery.accepted_at or accepted_at
    delivery.attempt_count = max(attempts, delivery.attempt_count)
    delivery.error_code = "" if accepted else "sma19_" + state
    delivery.save(update_fields=["status", "accepted_at", "attempt_count", "error_code"])
