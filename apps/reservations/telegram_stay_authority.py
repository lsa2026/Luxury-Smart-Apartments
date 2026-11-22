"""Website-owned, signed reads for one private stay job; never caller content."""

import uuid
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from . import telegram_authority as authority
from . import telegram_backend as backend
from . import telegram_workgroup as workgroup
from .telegram_stay_models import TelegramStayChannel, TelegramStayMessage, TelegramWorkgroupHandoff

READS = {"render_source", "delivery_status", "handoff_status", "review_policy", "binding"}
RECORD_SALT = "telegram39.stay-record.v1"


def scope_for(row, *, attempt=None):
    return {
        "kind": "stay_workgroup" if workgroup.is_handoff(row) else "stay_message",
        "job": str(row.pk),
        "attempt": attempt,
    }


def read(row, action, *, attempt=None):
    client = backend.Backend()
    try:
        client.authority_context = scope_for(row, attempt=attempt)
        return client.call(action, row.identity["stay_key"])
    finally:
        client.close()


def authorize(scope, action, key, result):
    flag = (
        "TELEGRAM_STAY_RECEIPTS_ENABLED"
        if action in {"delivery_status", "handoff_status"}
        else "TELEGRAM_STAY_MESSAGES_ENABLED"
    )
    if action not in READS or not getattr(settings, flag, False):
        raise authority.Denied
    model = (
        TelegramWorkgroupHandoff if scope.get("kind") == "stay_workgroup" else TelegramStayMessage
    )
    row = model.objects.get(pk=uuid.UUID(scope["job"]))
    if workgroup.is_handoff(row):
        if not workgroup.enabled() or action in {"binding", "delivery_status"}:
            raise authority.Denied
        result.update(dispatch_mode="workgroup_manual", workgroup_id=workgroup.WORKGROUP_ID)
    elif action == "handoff_status" or workgroup.enabled() and action != "delivery_status":
        raise authority.Denied
    identity = row.identity
    if (
        key != identity["stay_key"]
        or not TelegramStayChannel.objects.filter(
            scenario=row.scenario,
            stay_id=row.stay_id,
            hostaway_id=identity["hostaway_reservation_id"],
        ).exists()
    ):
        raise authority.Denied
    if action not in {"delivery_status", "handoff_status", "binding"}:
        # All current-domain locks end before the bridge reads its own context.
        from django.db import transaction

        from .telegram_stay_source import resolve

        with transaction.atomic():
            _, stay, event, _, _, _ = resolve(
                {
                    "contract": "tg39_stay_source_request_v1",
                    "scenario": row.scenario,
                    "operation": "review_next" if action == "review_policy" else "next",
                    "request_id": str(uuid.uuid4()),
                    "event_id": str(row.event_id),
                    "stay_key": key,
                }
            )
            from .telegram_stay_source import snapshot

            current = snapshot(
                {
                    "contract": "tg39_stay_source_request_v1",
                    "scenario": row.scenario,
                    "operation": "review_next" if action == "review_policy" else "next",
                    "request_id": str(uuid.uuid4()),
                    "event_id": str(event.pk),
                    "stay_key": key,
                }
            )
            if stay.communication_revision != row.revision or any(
                current["booking"][field] != identity[field]
                for field in ("phone_hash", "listing_map_id")
            ):
                raise authority.Denied
    result.update(identity)
    result["source_scope"] = "stay_review" if action == "review_policy" else "stay_message"
    if action == "handoff_status":
        result["source_scope"] = "stay_workgroup"
    if action in {"delivery_status", "handoff_status"}:
        attempt = scope.get("attempt")
        if attempt is not None and str(uuid.UUID(attempt)) != attempt:
            raise authority.Denied
        if row.attempt_id and attempt not in {None, str(row.attempt_id)}:
            raise authority.Denied
        result["attempt_id"] = attempt
    return result


def record_capability(row):
    return signing.dumps(
        {
            "job": str(row.pk),
            "content": row.content_sha256,
            **({"ledger": "workgroup"} if workgroup.is_handoff(row) else {}),
        },
        salt=RECORD_SALT,
        compress=True,
    )


@sensitive_variables()
def record_row(token):
    value = signing.loads(token, salt=RECORD_SALT, max_age=120)
    model = TelegramWorkgroupHandoff if value.get("ledger") == "workgroup" else TelegramStayMessage
    if workgroup.enabled() != (model is TelegramWorkgroupHandoff):
        raise authority.Denied
    row = model.objects.get(pk=uuid.UUID(value["job"]))
    if not row.content_sha256 or row.content_sha256 != value["content"]:
        raise authority.Denied
    return row


def packet_for(row, *, attempt):
    now = timezone.now()
    return {
        "contract": "tg39_stay_source_packet_v1",
        **workgroup.packet_fields(),
        "scenario": row.scenario,
        "source_owner": row.identity["source_owner"],
        "source_key": str(row.stay_id),
        "stay_key": row.identity["stay_key"],
        "hostaway_reservation_id": row.identity["hostaway_reservation_id"],
        "ownership_reference": row.booking_basis["ownership_reference"],
        "ownership_verified": True,
        "event_id": str(row.event_id),
        "source_revision": row.revision,
        "source_event_kind": row.booking_basis["source_event_kind"],
        "authoritative_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "booking": row.booking_basis["booking"],
        "decision": "record",
        "job": {
            "job_key": row.job_key,
            "reference_id": row.reference_id,
            "source_revision": row.revision,
            "purpose": row.purpose,
            "sequence": row.sequence,
            "recipient_kind": row.recipient_kind,
            "attempt_id": attempt,
            "source_capability": record_capability(row),
        },
    }
