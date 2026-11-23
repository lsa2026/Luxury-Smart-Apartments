"""Fresh, event-scoped READ at the original due time; never a guest sender.

Only newly captured events after an explicit cutover can be registered. There is
no backlog scanner, capability store, wait payload or automatic network retry.
"""

import re
from datetime import timedelta
from uuid import uuid5

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
V2_RELAY_URLS = {
    "07": "https://lsa25.app.n8n.cloud/webhook/lsa07-morning-stay-v1",
    "08": "https://lsa25.app.n8n.cloud/webhook/lsa08-day-use-booking-v1",
}
READ_HEADER = "X-LSA-Source-Read-Capability"
DEFERRED_REASONS = {"not_due", "waiting_predecessor", "deferred"}
DEFERRED_KEYS = {
    "contract_version",
    "relay_ref",
    "job_sha256",
    "accepted",
    "status",
    "reason",
    "request_id",
    "attempts_claimed",
    "provider_message_id",
    "wamid",
    "retryable_before_claim",
}
DEFER_RECHECK = timedelta(minutes=15)


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
    from .telegram_stay_transport import selected as telegram_selected

    if telegram_selected(event.stay):
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
        due = stay.ends_at - timedelta(hours=1) if kind == "reminder_checkout" else event.created_at
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
    if not financial and stay.status != "cancelled" and not stay.paid_at:
        register_payment_link(event, scenario)


def register_payment_link(event, scenario):
    """New source event only; creates a message resource, never a financial claim."""
    if not getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False) or not enabled():
        return
    from .telegram_stay_transport import selected
    from .website_financial_source_v2 import modules

    stay = source.SOURCES[scenario][0].objects.get(pk=event.stay_id)
    at = cutover()
    if (
        selected(stay)
        or event.created_at < at
        or event.created_at > timezone.now()
        or stay.environment != "production"
        or stay.status not in {"open", "expired"}
        or stay.paid_at
        or stay.ends_at <= timezone.now()
        or stay.communication_revision != event.revision
    ):
        return
    links, _ = modules(scenario)
    delivery, _ = stay.payment_link_deliveries.get_or_create(
        pk=uuid5(stay.pk, f"source-v2-payment-link-r{event.revision}"),
        defaults={"state": "waiting_core", "fingerprint": links.fingerprint(stay)},
    )
    if (
        delivery.created_at < at
        or delivery.state not in {"waiting_core", "queued"}
        or delivery.provider_id
        or delivery.completed_at
        or delivery.fingerprint != links.fingerprint(stay)
    ):
        return
    key = ("morning-pay-" if scenario == "07" else "day-use-pay-") + str(delivery.pk)
    WebsiteMessageRelay.objects.get_or_create(
        scenario=scenario,
        financial=False,
        event_id=delivery.pk,
        message_type="payment_link",
        defaults={
            "stay_id": stay.pk,
            "revision": str(event.revision),
            "job_sha256": source.sha(key),
            "source_occurred_at": delivery.created_at,
            "not_before": delivery.created_at,
            "expires_at": stay.ends_at,
        },
    )


def current_event(row):
    stays, events, pay_events, _, comms, payments = source.SOURCES[row.scenario]
    if row.message_type == "payment_link":
        if not getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False):
            return None
        from .website_financial_source_v2 import payment_link_source

        facts = payment_link_source(row.event_id, row.scenario)
        if not facts.get("available"):
            return None
        event = (
            events.objects.select_related("stay__property")
            .filter(pk=facts["booking_event_id"], stay_id=row.stay_id)
            .first()
        )
        if (
            event is None
            or facts["reservation_revision"] != row.revision
            or facts["job_sha256"] != row.job_sha256
            or event.created_at < cutover()
            or parse_datetime(facts["source_occurred_at"]) != row.source_occurred_at
            or parse_datetime(facts["expires_at"]) != row.expires_at
        ):
            return None
        return event, facts
    model = pay_events if row.financial else events
    event = model.objects.filter(pk=row.event_id, stay_id=row.stay_id).first()
    if event is None:
        return None
    from apps.properties.models import Property

    property_id = stays.objects.values_list("property_id", flat=True).get(pk=row.stay_id)
    Property.objects.select_for_update().get(pk=property_id)
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
    if (
        row.financial
        and row.message_type == "payment_success"
        and getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
    ):
        from .website_financial_source_v2 import payment_confirmation_source

        facts = payment_confirmation_source(event.pk, row.scenario)
        if not facts.get("available"):
            return None
    if not facts or facts["source_message_type"] != row.message_type:
        return None
    if (
        parse_datetime(facts["not_before"]) != row.not_before
        or parse_datetime(facts["source_occurred_at"]) != row.source_occurred_at
    ):
        return None
    return event, facts


@sensitive_variables()
def forward_due(*, client=None, limit=1):
    if not enabled():
        return {"accepted": 0}
    accepted = 0
    examined = set()
    budget = min(max(limit, 0), 3)
    while len(examined) < 30 and accepted < budget:
        with transaction.atomic():
            row = (
                WebsiteMessageRelay.objects.select_for_update(skip_locked=True)
                .filter(state="pending", not_before__lte=timezone.now())
                .exclude(
                    reason__in=["deferred_before_claim:" + reason for reason in DEFERRED_REASONS],
                    updated_at__gt=timezone.now() - DEFER_RECHECK,
                )
                .exclude(pk__in=examined)
                .order_by("updated_at", "not_before", "created_at")
                .first()
            )
            if row is None:
                break
            examined.add(row.pk)
            try:
                current = current_event(row)
            except (InvalidPhoneNumber, ValidationError, KeyError, ValueError, TypeError):
                current = None
            if current is None:
                row.state, row.reason = "stale", "source_changed_or_expired"
                row.save(update_fields=["state", "reason", "updated_at"])
                continue
            event, facts = current
            if not row.financial:
                from .message_receipts import core_slot_delivered

                if core_slot_delivered(event.stay, event, row.scenario, row.message_type):
                    row.state, row.reason = "satisfied", "verified_existing_delivery"
                    row.save(update_fields=["state", "reason", "updated_at"])
                    continue
            if not facts["domain_authorized"]:
                row.reason = "predecessor_or_domain_pending"
                row.save(update_fields=["reason", "updated_at"])
                continue  # Wait for actual predecessor delivery, not an ACK.
            row.state = "forwarding"
            row.save(update_fields=["state", "updated_at"])
        # Durable fence is committed BEFORE the network. A crash/timeout is not
        # retried, even if no ACK was received. Reconciliation is separate.
        comms, payments = source.SOURCES[row.scenario][4:6]
        capability = payments.capability(event) if row.financial else comms.event_token(event)
        envelope = {
            "contract_version": "site01-source-relay-2"
            if getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
            else "site01-source-relay-1",
            "relay_ref": str(row.pk),
            "scenario": row.scenario,
            "source_kind": "payment_link"
            if row.message_type == "payment_link"
            else "payment"
            if row.financial
            else "core",
            "source_event_id": str(row.event_id),
            "source_event_key": facts["source_event_key"],
            "source_occurred_at": row.source_occurred_at.isoformat(),
            "job_sha256": row.job_sha256,
            "source_message_type": row.message_type,
            "reservation_revision": row.revision,
            "not_before": row.not_before.isoformat(),
            "expires_at": row.expires_at.isoformat(),
        }
        state, reason, request_id = "uncertain", "ack_unverified", ""
        target_url = (
            V2_RELAY_URLS.get(row.scenario, RELAY_URL)
            if getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
            else RELAY_URL
        )
        try:
            if client is None:
                with httpx.Client(
                    timeout=httpx.Timeout(40, connect=5), follow_redirects=False
                ) as remote:
                    reply = remote.post(
                        target_url, json=envelope, headers={READ_HEADER: capability}
                    )
            else:
                reply = client.post(target_url, json=envelope, headers={READ_HEADER: capability})
            body = reply.json() if reply.status_code in {200, 202} else {}
            if (
                isinstance(body, dict)
                and body.get("contract_version")
                == (
                    "site01-source-relay-ack-2"
                    if getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
                    else "site01-source-relay-ack-1"
                )
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
                elif (
                    getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
                    and set(body) == DEFERRED_KEYS
                    and body.get("accepted") is False
                    and body.get("status") == "deferred"
                    and body.get("reason") in DEFERRED_REASONS
                    and body.get("request_id") is None
                    and type(body.get("attempts_claimed")) is int
                    and body["attempts_claimed"] == 0
                    and body.get("provider_message_id") is None
                    and body.get("wamid") is None
                    and body.get("retryable_before_claim") is True
                ):
                    state, reason = "pending", "deferred_before_claim:" + body["reason"]
        except Exception:
            # Do not log request headers, response bodies or transport exceptions.
            pass
        WebsiteMessageRelay.objects.filter(pk=row.pk, state="forwarding").update(
            state=state, reason=reason, request_id=request_id, updated_at=timezone.now()
        )
        if state != "accepted":
            break  # One network attempt per default worker round, including held.
    return {"accepted": accepted}
