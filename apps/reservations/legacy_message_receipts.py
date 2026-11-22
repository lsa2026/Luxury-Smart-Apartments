"""Bound historical UltraMsg delivery evidence; never a central receipt or sender.

Cache only an authenticated provider GET already used by the original domain
gate. Existing Django-key MACs bind the proof to the exact persisted attempt;
neither acceptance nor arbitrary response JSON can become delivery evidence.
"""

import json

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.dateparse import parse_datetime

KEY = "_site01_verified_legacy_delivery_v1"
ACKS = {"2", "3", "4", "device", "delivered", "read", "played"}
STATES = {"delivered", "read", "played"}


def is_core_attempt(attempt):
    from .models import DayUse08DeliveryAttempt, Morning07DeliveryAttempt

    return type(attempt) in {Morning07DeliveryAttempt, DayUse08DeliveryAttempt}


def mac(value):
    return salted_hmac(
        "website-legacy-delivery.v1",
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        algorithm="sha256",
    ).hexdigest()


def identity(attempt):
    return mac(
        [
            settings.ULTRAMSG_INSTANCE_ID,
            attempt._meta.label_lower,
            attempt.pk,
            str(attempt.stay_id),
            attempt.revision,
            attempt.provider_id,
            attempt.payload_hash,
            attempt.created_at.isoformat(),
        ]
    )


def bound_before_cutover(attempt):
    if not is_core_attempt(attempt) or not attempt.provider_id:
        return False
    try:
        cutover = parse_datetime(getattr(settings, "MESSAGE_SOURCE_CUTOVER_AT", ""))
    except (TypeError, ValueError):
        return False
    if not cutover or not timezone.is_aware(cutover) or cutover > timezone.now():
        return False
    return (
        type(attempt)
        .objects.filter(
            pk=attempt.pk,
            stay_id=attempt.stay_id,
            revision=attempt.revision,
            provider_id=attempt.provider_id,
            payload_hash=attempt.payload_hash,
            created_at=attempt.created_at,
            created_at__lt=cutover,
            state__in=["accepted", "posting", "uncertain"],
        )
        .exists()
    )


def delivered(row):
    return str(row.get("ack", "")).lower() in ACKS or str(row.get("status", "")).lower() in STATES


def cached_rows(attempt):
    if not is_core_attempt(attempt) or not isinstance(attempt.response, dict):
        return []
    proof = attempt.response.get(KEY)
    if not isinstance(proof, dict) or set(proof) != {
        "row",
        "identity_mac",
        "observed_at_epoch",
        "proof_mac",
    }:
        return []
    content = {k: v for k, v in proof.items() if k != "proof_mac"}
    row = content["row"]
    observed = content["observed_at_epoch"]
    if (
        not isinstance(row, dict)
        or set(row) != {"id", "referenceId", "ack", "status"}
        or not attempt.provider_id
        or row.get("id") != attempt.provider_id
        or row.get("referenceId") not in {"", attempt.reference_id}
        or not delivered(row)
        or type(observed) is not int
        or not int(attempt.created_at.timestamp())
        <= observed
        <= int(timezone.now().timestamp()) + 30
        or not constant_time_compare(str(content["identity_mac"]), identity(attempt))
        or not constant_time_compare(str(proof["proof_mac"]), mac(content))
    ):
        return []
    return [row]


def remember(attempt, rows):
    if not is_core_attempt(attempt) or not attempt.provider_id:
        return
    row = next((r for r in rows if r.get("id") == attempt.provider_id and delivered(r)), None)
    if row is None:
        return
    normalized = {k: str(row.get(k) or "") for k in ("id", "referenceId", "ack", "status")}
    if normalized["referenceId"] not in {"", attempt.reference_id}:
        return
    content = {
        "row": normalized,
        "identity_mac": identity(attempt),
        "observed_at_epoch": int(timezone.now().timestamp()),
    }
    proof = {**content, "proof_mac": mac(content)}
    with transaction.atomic():
        current = type(attempt).objects.select_for_update().get(pk=attempt.pk)
        if identity(current) != identity(attempt):
            return
        response = current.response if isinstance(current.response, dict) else {}
        current.response = {**response, KEY: proof}
        current.save(update_fields=["response", "updated_at"])
        attempt.response = current.response
