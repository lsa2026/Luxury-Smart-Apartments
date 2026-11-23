"""Current terminal-extension checkout source; no provider or financial writes.

READ capabilities reuse the website signer, but have a separate strict action
scope and120-second lifetime. An old ownership/event token is not this authority.
"""

import re
from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from apps.core.phone_numbers import normalize_phone_number
from apps.properties.models import Property

from . import message_source_authorization as source
from .stay_extension_communications import SALT

FAMILY = "lsa_ext_checkout"
SCENARIO = "02"
ACTION = "extension_checkout_source_v2"
HEADER = "X-LSA-Extension-Token"
READ_PATH = "/integrations/stay-extensions/checkout/source-authorization/"
CLAIM_KEYS = {
    "action",
    "relay_ref",
    "extension_id",
    "event_id",
    "chain_id",
    "revision",
    "chain_fingerprint",
    "job_sha256",
    "issued_at",
    "expires_at",
}


def cutover():
    try:
        value = parse_datetime(getattr(settings, "MESSAGE_EXTENSION_SOURCE_CUTOVER_AT", ""))
    except (ValueError, TypeError):
        return None
    return value if value and timezone.is_aware(value) else None


def enabled():
    return bool(
        getattr(settings, "MESSAGE_EXTENSION_SOURCE_V2_ENABLED", False)
        and getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
        and getattr(settings, "MESSAGE_SOURCE_AUTH_ENABLED", False)
        and getattr(settings, "WEBSITE_GUEST_MESSAGE_TRANSPORT", "") == "ycloud_central"
        and cutover() is not None
    )


def original_schedule(effective_checkout_at, zone_name):
    end = datetime.fromisoformat(effective_checkout_at)
    if not timezone.is_aware(end):
        raise ValueError("extension_checkout_timezone_missing")
    zone = ZoneInfo(zone_name)
    local = end.astimezone(zone)
    due = datetime.combine(local.date() - timedelta(days=1), time(21), zone)
    return due.astimezone(UTC), end.astimezone(UTC)


def job_key(chain_id, terminal_id, effective_checkout_at, zone_name):
    chain_id = UUID(str(chain_id))
    terminal_id = str(terminal_id)
    if not terminal_id.isascii() or not terminal_id.isdigit() or int(terminal_id) <= 0:
        raise ValueError("extension_terminal_invalid")
    end = datetime.fromisoformat(effective_checkout_at)
    if not timezone.is_aware(end):
        raise ValueError("extension_checkout_timezone_missing")
    departure = end.astimezone(ZoneInfo(zone_name)).date().isoformat()
    return f"extension:checkout:{chain_id}:{terminal_id}:{departure}"


def read_capability(row, extension, *, now=None):
    now = now or timezone.now()
    expires = min(now + timedelta(seconds=120), row.expires_at)
    if (
        not row.not_before <= now < expires
        or row.state != "forwarding"
        or row.stay_id != extension.chain_id
        or row.revision != str(extension.chain.revision)
    ):
        raise ValueError("extension_source_not_due")
    return signing.dumps(
        {
            "action": ACTION,
            "relay_ref": str(row.pk),
            "extension_id": str(extension.pk),
            "event_id": str(row.event_id),
            "chain_id": str(extension.chain_id),
            "revision": row.revision,
            "chain_fingerprint": extension.chain.fingerprint,
            "job_sha256": row.job_sha256,
            "issued_at": int(now.timestamp()),
            "expires_at": int(expires.timestamp()),
        },
        salt=SALT,
    )


def read_claims(capability, job_sha256, *, now=None):
    now = int((now or timezone.now()).timestamp())
    claims = signing.loads(capability, salt=SALT, max_age=120)
    if (
        not isinstance(claims, dict)
        or set(claims) != CLAIM_KEYS
        or claims.get("action") != ACTION
        or not isinstance(job_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", job_sha256)
        or not isinstance(claims.get("job_sha256"), str)
        or not source.constant_time_compare(claims.get("job_sha256", ""), job_sha256)
        or type(claims.get("issued_at")) is not int
        or type(claims.get("expires_at")) is not int
        or not claims["issued_at"] <= now < claims["expires_at"]
        or not 0 < claims["expires_at"] - claims["issued_at"] <= 120
        or not isinstance(claims.get("revision"), str)
        or not re.fullmatch(r"[1-9][0-9]{0,9}", claims["revision"])
        or not isinstance(claims.get("chain_fingerprint"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", claims["chain_fingerprint"])
    ):
        raise ValueError("extension_source_capability_invalid")
    for key in ("relay_ref", "extension_id", "event_id", "chain_id"):
        if not isinstance(claims[key], str) or str(UUID(claims[key])) != claims[key]:
            raise ValueError("extension_source_capability_invalid")
    return claims


@sensitive_variables()
def current_basis(event_id):
    """Verify the original current chain/live source, without changing any row.

    The wire identity is projected separately against source20's current mirror.
    Neither a supplied context nor a cached lookup substitutes for this read.
    """
    from . import stay_extension_communications as comms
    from .models import StayExtension, StayExtensionChain, StayExtensionEvent

    pointer = (
        StayExtensionEvent.objects.filter(pk=event_id)
        .values("extension_id", "extension__chain_id", "extension__chain__property_id")
        .first()
    )
    if not pointer:
        raise ValueError("extension_source_event_missing")
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=pointer["extension__chain__property_id"])
        chain = (
            StayExtensionChain.objects.select_for_update()
            .select_related("property")
            .get(pk=pointer["extension__chain_id"])
        )
        extension = (
            StayExtension.objects.select_for_update(of=("self",))
            .select_related("draft", "reservation__booking_intent")
            .get(pk=pointer["extension_id"], chain=chain)
        )
        extension.chain = chain
        event = StayExtensionEvent.objects.select_for_update().get(pk=event_id, extension=extension)
        event.extension = extension
        if (
            chain.property_id != pointer["extension__chain__property_id"]
            or event.revision != chain.revision
            or not extension.ownership_acknowledged
            or extension.state != "active"
            or not extension.reservation_id
            or extension.reservation.is_test
        ):
            raise ValueError("extension_source_revision_not_current")
        context = comms.context(extension)  # verify_live=True, never a caller flag.
        terminal = str(context.get("terminal_hostaway_reservation_id"))
        current = str(extension.reservation.hostaway_reservation_id)
        members = context.get("chain_members")
        if (
            context.get("communication_ready") is not True
            or context.get("active_linkage_verified") is not True
            or context.get("chain_state") != "active"
            or context.get("booking_verified") is not True
            or context.get("payment_verified") is not True
            or context.get("chain_revision") != chain.revision
            or context.get("chain_fingerprint") != chain.fingerprint
            or terminal != current
            or not isinstance(members, list)
            or len([m for m in members if m.get("allow_checkout") is True]) != 1
            or not any(
                str(m.get("hostaway_reservation_id")) == terminal
                and m.get("allow_checkout") is True
                for m in members
            )
        ):
            raise ValueError("extension_terminal_not_verified")
        contact = context["communication_snapshot"]
        phone = normalize_phone_number(contact["phone"])
        if not contact.get("name") or contact.get("language") not in {"ar", "en", "fr"}:
            raise ValueError("extension_recipient_not_verified")
        due, end = original_schedule(context["effective_checkout_at"], context["timezone"])
        if end <= timezone.now():
            raise ValueError("extension_source_expired")
        key = job_key(chain.pk, terminal, context["effective_checkout_at"], context["timezone"])
        return event, extension, context, phone, key, due, end


def register_event(event):
    """Fresh source capture only, preserving due even for a new late event.

    The event cutover and current expiry fence history; a past original due
    does not make a newly captured, unattempted source event historical.
    """
    if not enabled():
        return
    from .message_relay_models import WebsiteMessageRelay
    from .models import StayExtension
    from .stay_extensions import chain_context

    extension = StayExtension.objects.select_related(
        "chain__property", "draft", "reservation__booking_intent"
    ).get(pk=event.extension_id)
    state = chain_context(extension.chain)
    due, end = original_schedule(state["effective_checkout_at"], state["timezone"])
    if (
        event.created_at < cutover()
        or event.created_at > timezone.now()
        or event.revision != extension.chain.revision
        or end <= timezone.now()
        or extension.state != "active"
        or not extension.ownership_acknowledged
        or not extension.reservation_id
        or state.get("active_linkage_verified") is not True
        or str(state["terminal_hostaway_reservation_id"])
        != str(extension.reservation.hostaway_reservation_id)
    ):
        return
    key = job_key(
        extension.chain_id,
        state["terminal_hostaway_reservation_id"],
        state["effective_checkout_at"],
        state["timezone"],
    )
    WebsiteMessageRelay.objects.get_or_create(
        scenario=SCENARIO,
        financial=False,
        event_id=event.pk,
        message_type="reminder_checkout",
        defaults={
            "stay_id": extension.chain_id,
            "revision": str(event.revision),
            "job_sha256": source.sha(key),
            "source_occurred_at": event.created_at,
            "not_before": due,
            "expires_at": end,
        },
    )


@sensitive_variables()
def facts_for_row(row):
    from .message_receipt_models import WebsiteMessageExpectation
    from .message_receipts import projected_expectation

    event, extension, context, phone, key, due, end = current_basis(row.event_id)
    if (
        row.scenario != SCENARIO
        or row.financial
        or row.message_type != "reminder_checkout"
        or row.stay_id != extension.chain_id
        or row.revision != str(event.revision)
        or row.job_sha256 != source.sha(key)
        or row.source_occurred_at != event.created_at
        or row.not_before != due
        or row.expires_at != end
        or cutover() is None
        or event.created_at < cutover()
    ):
        raise ValueError("extension_source_journal_changed")
    terminal = str(context["terminal_hostaway_reservation_id"])
    local_end = end.astimezone(ZoneInfo(context["timezone"]))
    contact = context["communication_snapshot"]
    prior_attempt = (
        WebsiteMessageExpectation.objects.filter(
            relay__scenario=SCENARIO,
            relay__stay_id=extension.chain_id,
            relay__job_sha256=row.job_sha256,
            attempts_claimed__gt=0,
        )
        .exclude(relay=row)
        .exists()
    )
    facts = {
        "contract_version": "yc19-source-facts-1",
        "source_family": FAMILY,
        "source_owner_family": "website",
        "scenario": SCENARIO,
        "job_sha256": row.job_sha256,
        "source_event_key": "website:extension:checkout:" + str(event.pk),
        "source_occurred_at": event.created_at.isoformat(),
        "reservation_key": terminal,
        "reservation_revision": str(event.revision),
        "source_message_type": "reminder_checkout",
        "purpose": "checkout_reminder",
        "recipient_e164": phone,
        "recipient_ref": "site01:"
        + source.salted_hmac(
            "website-source-recipient.v1",
            f"extension:{extension.chain_id}:{phone}",
            algorithm="sha256",
        ).hexdigest(),
        "semantic_key": source.sha(key),
        "semantic_key_version": "website-extension-checkout-slot-v1",
        "language": contact["language"],
        "not_before": due.isoformat(),
        "expires_at": end.isoformat(),
        "environment": "production",
        "current_source_verified": True,
        "current_source_cancelled": False,
        "domain_authorized": bool(
            enabled()
            and due <= timezone.now() < end
            and row.state in {"pending", "forwarding"}
            and not prior_attempt
        ),
        "legacy_state": "unknown",
        "legacy_history_owner": "source20",
        "consent": dict.fromkeys(("service", "marketing", "purpose", "evidence_ref", "revoked")),
        "human_takeover": None,
        "message": None,
        "render_policy_ref": None,
        "dispatch_allowed": False,
        "chain_id": str(extension.chain_id),
        "chain_revision": str(event.revision),
        "chain_fingerprint": extension.chain.fingerprint,
        "terminal_hostaway_reservation_id": terminal,
        "variables": {
            "guest_name": contact["name"],
            "booking_reference": terminal,
            "listing_name": contact["listing_name"],
            "departure_date": local_end.date().isoformat(),
            "check_out_time": local_end.strftime("%H:%M"),
            "timezone": context["timezone"],
            "timezone_display": context["timezone"],
        },
        "blockers": ["legacy_history_requires_source20", "human_control_requires_owner_ledger"],
    }
    facts["receipt_expectation"] = projected_expectation(facts)
    return event, source.scoped_facts(facts)


@sensitive_variables()
def policy_from_source(event, facts, context):
    from . import website_template_selector_v2 as selector
    from .message_relay_models import WebsiteMessageRelay

    if (
        not isinstance(context, dict)
        or context.get("legacy_state") != "not_attempted"
        or context.get("preflight_status") != "ready"
        or not isinstance(context.get("legacy_evidence_ref"), str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", context["legacy_evidence_ref"])
    ):
        return None
    row = WebsiteMessageRelay.objects.get(
        scenario=SCENARIO,
        event_id=event.pk,
        job_sha256=facts["job_sha256"],
        financial=False,
        message_type="reminder_checkout",
    )
    _, current = facts_for_row(row)
    if not current["domain_authorized"]:
        return None
    if any(
        current.get(k) != facts.get(k)
        for k in (
            "reservation_key",
            "reservation_revision",
            "source_event_key",
            "recipient_ref",
            "recipient_e164",
            "purpose",
            "source_message_type",
            "language",
            "job_sha256",
        )
    ):
        return None
    if context.get("source_family") != FAMILY:
        return None
    bound = selector.match_context(current, context)
    if not isinstance(bound.get("values"), dict) or any(
        bound["values"].get(key) != value for key, value in current["variables"].items()
    ):
        return None
    selected = selector.select_source_message(current, bound)
    return {
        "language": current["language"],
        "message": selected.message,
        "render_policy_ref": selected.selection_ref,
    }


@csrf_exempt
@sensitive_variables()
def authorization(request):
    import json

    from .message_relay_models import WebsiteMessageRelay

    request._disable_google_integrations = True
    capability = request.META.pop("HTTP_X_LSA_EXTENSION_TOKEN", "")
    request.__dict__.pop("headers", None)
    if not enabled():
        return source.response({"code": "unavailable"}, 404)
    if request.method != "POST":
        return source.response({"code": "method_not_allowed"}, 405)
    if request.GET or request.content_type != "application/json" or len(request.body) > 1024:
        return source.response({"code": "invalid_request"}, 400)
    try:
        body = json.loads(request.body, object_pairs_hook=source.unique_object)
        if not isinstance(body, dict) or set(body) != {"job_sha256"}:
            raise ValueError
        claims = read_claims(capability, body["job_sha256"])
        with transaction.atomic():
            row = WebsiteMessageRelay.objects.select_for_update().get(
                pk=claims["relay_ref"],
                scenario=SCENARIO,
                event_id=claims["event_id"],
                stay_id=claims["chain_id"],
                revision=claims["revision"],
                job_sha256=claims["job_sha256"],
                financial=False,
                message_type="reminder_checkout",
            )
            event, facts = facts_for_row(row)
            if (
                str(event.extension_id) != claims["extension_id"]
                or facts["chain_fingerprint"] != claims["chain_fingerprint"]
            ):
                raise ValueError
    except Exception:
        return source.response({"code": "unavailable", "dispatch_allowed": False}, 404)
    return source.response(facts)


def historical_scope(row):
    from .models import StayExtensionEvent

    if not StayExtensionEvent.objects.filter(
        pk=row.event_id, extension__chain_id=row.stay_id, revision=row.revision
    ).exists():
        raise ValueError("extension_historical_scope_mismatch")
    return {
        "scenario": SCENARIO,
        "source_family": FAMILY,
        "job_sha256": row.job_sha256,
        "source_event_key": "website:extension:checkout:" + str(row.event_id),
        "reservation_revision": row.revision,
        "source_message_type": "reminder_checkout",
        "purpose": "checkout_reminder",
    }


def initial_relay(proof):
    from .message_relay_models import WebsiteMessageRelay
    from .models import StayExtensionEvent

    event_key = proof.get("source_event_key")
    match = re.fullmatch(r"website:extension:checkout:([0-9a-f-]{36})", event_key or "")
    reservation = proof.get("reservation_key")
    if (
        not match
        or proof.get("source_family") != FAMILY
        or proof.get("scenario") != SCENARIO
        or proof.get("purpose") != "checkout_reminder"
        or proof.get("source_message_type") != "reminder_checkout"
        or not isinstance(reservation, str)
        or not re.fullmatch(r"[1-9][0-9]{0,19}", reservation)
        or not isinstance(proof.get("reservation_revision"), str)
        or not re.fullmatch(r"[1-9][0-9]{0,9}", proof["reservation_revision"])
        or not isinstance(proof.get("job_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", proof["job_sha256"])
    ):
        raise ValueError("extension_binding_scope_mismatch")
    pointer = StayExtensionEvent.objects.values("extension__chain_id").get(pk=UUID(match[1]))
    rows = list(
        WebsiteMessageRelay.objects.filter(
            scenario=SCENARIO,
            financial=False,
            event_id=UUID(match[1]),
            stay_id=pointer["extension__chain_id"],
            revision=proof["reservation_revision"],
            job_sha256=proof["job_sha256"],
            message_type="reminder_checkout",
            state="forwarding",
        ).values_list("pk", flat=True)[:2]
    )
    if len(rows) != 1:
        raise ValueError("extension_binding_scope_missing")
    return rows[0]
