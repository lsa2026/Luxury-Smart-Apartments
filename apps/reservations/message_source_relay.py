"""Fresh, event-scoped READ at the original due time; never a guest sender.

Only newly captured events after an explicit cutover can be registered. There is
no backlog scanner, capability store, wait payload or automatic network retry.
"""

import re
from datetime import timedelta

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from apps.core.phone_numbers import InvalidPhoneNumber

from . import message_source_authorization as source
from .message_relay_models import WebsiteMessageRelay

RELAY_URL = "https://lsa25.app.n8n.cloud/webhook/site01-source-relay-v1"
READ_HEADER = "X-LSA-Source-Read-Capability"


def cutover():
    try:
        value = parse_datetime(getattr(settings, "MESSAGE_SOURCE_CUTOVER_AT", ""))
    except (ValueError, TypeError):
        return None
    return value if value and timezone.is_aware(value) else None


def enabled():
    return (
        getattr(settings, "MESSAGE_SOURCE_RELAY_ENABLED", False)
        and getattr(settings, "MESSAGE_SOURCE_AUTH_ENABLED", False)
        and getattr(settings, "WEBSITE_GUEST_MESSAGE_TRANSPORT", "ultramsg") == "ycloud_central"
        and cutover() is not None
    )


def register_event(event, scenario, *, financial=False):
    """Called in the source capture transaction, not from a caller's locator."""
    if not enabled():
        return
    start = cutover()
    stay = event.stay
    if (
        event.created_at < start
        or event.created_at > timezone.now()
        or stay.ends_at <= timezone.now()
    ):
        return
    if financial:
        if not stay.paid_at or stay.paid_at < start:
            return
        payments = source.SOURCES[scenario][5]
        keys = {kind: payments.reference(event.pk, kind)[0] for kind in payments.KINDS}
        occurred_at = stay.paid_at
    else:
        keys = source.core_keys(stay.pk, event.payload, scenario)
        occurred_at = event.created_at
    for kind, key in keys.items():
        if stay.status == "cancelled" and kind != "status_cancelled":
            continue
        due = stay.ends_at - timedelta(hours=1) if kind == "reminder_checkout" else occurred_at
        # A new event must not awaken a checkout reminder already due in the past.
        if kind == "reminder_checkout" and due < event.created_at:
            continue
        WebsiteMessageRelay.objects.get_or_create(
            scenario=scenario,
            financial=financial,
            event_id=event.pk,
            message_type=kind,
            defaults={
                "stay_id": stay.pk,
                "revision": str(
                    event.payload["booking_revision" if financial else "source_revision"]
                ),
                "job_sha256": source.sha(key),
                "source_occurred_at": occurred_at,
                "not_before": due,
                "expires_at": stay.ends_at,
            },
        )


def current_event(row):
    stays, events, pay_events, _, comms, payments = source.SOURCES[row.scenario]
    model = pay_events if row.financial else events
    event = model.objects.filter(pk=row.event_id, stay_id=row.stay_id).first()
    if event is None:
        return None
    stay = (
        stays.objects.select_for_update(of=("self",)).select_related("property").get(pk=row.stay_id)
    )
    event.stay = stay
    if not (
        payments.enabled()
        if row.financial
        else (
            settings.MORNING07_N8N_ENABLED if row.scenario == "07" else settings.DAY_USE08_ENABLED
        )
    ):
        return None
    if (
        stay.environment != "production"
        or not comms.admitted(stay)
        or stay.ends_at != row.expires_at
        or stay.ends_at <= timezone.now()
        or str(stay.communication_revision) != row.revision
        or event.created_at < cutover()
    ):
        return None
    valid = payments.current(event) if row.financial else source.current_core(stay, event, comms)
    if not valid:
        return None
    if row.financial and (
        event.payload.get("payment_event_id") != str(event.pk)
        or event.payload.get("payment_revision") != event.payment_revision
        or event.payload.get("proof_key") != event.proof_key
    ):
        return None
    facts = (
        source.payment_facts(stay, event, row.scenario, row.job_sha256, payments)
        if row.financial
        else source.core_facts(stay, event, row.scenario, row.job_sha256)
    )
    if not facts or facts["source_message_type"] != row.message_type:
        return None
    if (
        parse_datetime(facts["not_before"]) != row.not_before
        or parse_datetime(facts["source_occurred_at"]) != row.source_occurred_at
    ):
        return None
    return event, facts


@sensitive_variables()
def forward_due(*, client=None, limit=3):
    if not enabled():
        return {"accepted": 0}
    accepted = 0
    for _ in range(min(max(limit, 0), 3)):
        with transaction.atomic():
            row = (
                WebsiteMessageRelay.objects.select_for_update(skip_locked=True)
                .filter(state="pending", not_before__lte=timezone.now())
                .order_by("not_before", "created_at")
                .first()
            )
            if row is None:
                break
            try:
                current = current_event(row)
            except (InvalidPhoneNumber, ValidationError, KeyError, ValueError, TypeError):
                current = None
            if current is None:
                row.state, row.reason = "stale", "source_changed_or_expired"
                row.save(update_fields=["state", "reason", "updated_at"])
                continue
            event, facts = current
            row.state = "forwarding"
            row.save(update_fields=["state", "updated_at"])
        # Durable fence is committed BEFORE the network. A crash/timeout is not
        # retried, even if no ACK was received. Reconciliation is separate.
        comms, payments = source.SOURCES[row.scenario][4:6]
        capability = payments.capability(event) if row.financial else comms.event_token(event)
        envelope = {
            "contract_version": "site01-source-relay-1",
            "relay_ref": str(row.pk),
            "scenario": row.scenario,
            "source_kind": "payment" if row.financial else "core",
            "source_event_id": str(event.pk),
            "source_event_key": facts["source_event_key"],
            "source_occurred_at": row.source_occurred_at.isoformat(),
            "job_sha256": row.job_sha256,
            "source_message_type": row.message_type,
            "reservation_revision": row.revision,
            "not_before": row.not_before.isoformat(),
            "expires_at": row.expires_at.isoformat(),
        }
        state, reason, request_id = "uncertain", "ack_unverified", ""
        try:
            if client is None:
                with httpx.Client(
                    timeout=httpx.Timeout(15, connect=5), follow_redirects=False
                ) as remote:
                    reply = remote.post(RELAY_URL, json=envelope, headers={READ_HEADER: capability})
            else:
                reply = client.post(RELAY_URL, json=envelope, headers={READ_HEADER: capability})
            body = reply.json() if reply.status_code in {200, 202} else {}
            if (
                isinstance(body, dict)
                and body.get("contract_version") == "site01-source-relay-ack-1"
                and body.get("relay_ref") == str(row.pk)
                and body.get("job_sha256") == row.job_sha256
            ):
                candidate = body.get("request_id")
                if (
                    body.get("accepted") is True
                    and body.get("status") in {"bound", "delivered"}
                    and isinstance(candidate, str)
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", candidate)
                ):
                    state, reason, request_id = "accepted", "", candidate
                    accepted += 1
                elif body.get("accepted") is False and body.get("status") == "held":
                    state, reason = "held", "downstream_held"
        except Exception:
            # Do not log request headers, response bodies or transport exceptions.
            pass
        WebsiteMessageRelay.objects.filter(pk=row.pk, state="forwarding").update(
            state=state, reason=reason, request_id=request_id, updated_at=timezone.now()
        )
    return {"accepted": accepted}
