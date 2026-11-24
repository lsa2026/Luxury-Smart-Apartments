"""Independent permanent review slots and strictly future source capture.

Technical scheduling is private metadata; only the existing authenticated
zero-claim prepare/ACK can bind a slot. Google controls come independently
from the private runtime policy view, never a caller or a team receipt.
"""

import json
import re
from datetime import datetime, time, timedelta
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
from . import message_source_relay as relay
from .message_relay_models import WebsiteMessageRelay
from .review_source_models import WebsiteReviewCampaign, WebsiteReviewSlot
from .website_review_policy_v2 import LOCAL_VALUES, compose_review_context

TAG = "site01-independent-review-1"
POLICY_REF = "yc20-website-review-policy-v2:20261124.1"
SALT = "reservations.independent-review-job-read.v2"
KINDS = {f"review_followup_{step}": step for step in (1, 2, 3)}
REVIEW_HISTORY = tuple(KINDS) + (
    "review_request",
    "review_reminder_1",
    "review_reminder_2",
    "review_reminder_3",
)


def schedule(ends_at, zone_name):
    if not timezone.is_aware(ends_at):
        raise ValueError("review_end_requires_zone")
    zone = ZoneInfo(zone_name)
    local = ends_at.astimezone(zone)
    day = local.date()

    def at(offset, hour):
        return datetime.combine(day + timedelta(days=offset), time(hour), zone)

    slots = (max(ends_at, at(0, 15)), at(1, 9), at(1, 12))
    expiry = at(2, 0)
    if not slots[0] < slots[1] < slots[2] < expiry:
        raise ValueError("review_original_slots_not_distinct")
    return slots, expiry


def scope(stay, event, scenario):
    phone = normalize_phone_number(stay.guest_phone)
    return {
        "scenario": scenario,
        "stay_id": str(stay.pk),
        "reservation_key": ("morning07:" if scenario == "07" else "dayuse08:") + str(stay.pk),
        "reservation_revision": str(stay.communication_revision),
        "recipient_binding_sha256": source.sha(phone),
        "listing_map_id": str(stay.property.hostaway_listing_map_id),
        "ends_at": stay.ends_at.isoformat(),
        "time_zone": stay.time_zone,
        "language": stay.guest_language,
        "core_source_event_id": str(event.pk),
    }


def locked_stay(scenario, stay_id):
    model = source.SOURCES[scenario][0]
    property_id = model.objects.values_list("property_id", flat=True).get(pk=stay_id)
    Property.objects.select_for_update().get(pk=property_id)
    return model.objects.select_for_update(of=("self",)).select_related("property").get(pk=stay_id)


def technical_stay(scenario, stay_id):
    # Capture already owns the stay lock. Technical metadata must not reverse
    # the property's preclaim lock order or authorize any actual dispatch.
    return (
        source.SOURCES[scenario][0]
        .objects.select_for_update(of=("self",))
        .select_related("property")
        .get(pk=stay_id)
    )


def cutover():
    try:
        at = parse_datetime(getattr(settings, "MESSAGE_REVIEW_SOURCE_CUTOVER_AT", ""))
    except (ValueError, TypeError):
        return None
    return at if at and timezone.is_aware(at) else None


def enabled():
    at, general = cutover(), relay.cutover()
    return bool(
        getattr(settings, "MESSAGE_REVIEW_SOURCE_V2_ENABLED", False) is True
        and getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False) is True
        and relay.enabled()
        and at
        and general
        and general <= at <= timezone.now()
    )


def capture_new_core(event, scenario):
    """Called only for a newly inserted, typed original core event, never a scan."""
    if not enabled():
        return None
    campaign = create_technical_slots(event, scenario)
    if campaign is not None:
        for slot in campaign.slots.order_by("step"):
            register_slot(slot.pk)
    return campaign


def capture_new_payment(event, scenario):
    """New verified payment may complete a new-cohort current core admission."""
    if not enabled():
        return None
    if scenario not in source.SOURCES or not isinstance(event, source.SOURCES[scenario][2]):
        raise ValueError("review_payment_owner_mismatch")
    if not cutover() <= event.created_at <= timezone.now():
        return None
    # Current core itself must belong to the new review cohort. A new payment
    # never backfills an older booking or changes the original source timestamp.
    stay = technical_stay(scenario, event.stay_id)
    payments = source.SOURCES[scenario][5]
    if not payments.current(event):
        return None
    core = stay.latest_communication_event()
    return capture_new_core(core, scenario) if core is not None else None


def local_history(stay, scenario):
    from .telegram_stay_models import (
        TelegramStayChannel,
        TelegramStayMessage,
        TelegramStayNotice,
        TelegramWorkgroupHandoff,
    )

    # A selected prior channel is ownership, even without a local attempt marker.
    if TelegramStayChannel.objects.filter(scenario=scenario, stay_id=stay.pk).exists():
        return "unknown"
    if TelegramStayNotice.objects.filter(scenario=scenario, stay_id=stay.pk).exists():
        return "unknown"
    for model in (TelegramStayMessage, TelegramWorkgroupHandoff):
        if model.objects.filter(
            scenario=scenario, stay_id=stay.pk, purpose__in=REVIEW_HISTORY
        ).exists():
            return "unknown"
    # The old attempt table has no purpose field; its absence cannot prove
    # which historical actions were attempted. Any such opaque row holds.
    if source.SOURCES[scenario][3].objects.filter(stay=stay).exists():
        return "unknown"
    return "not_attempted"


def payment_eligible(stay, scenario):
    # Preserve the financial source's exact full-collection, amount/currency,
    # unresolved-attempt and confirmed-booking checks even after checkout.
    return source.SOURCES[scenario][5].verified_facts(stay, allow_ended=True) is not None


@transaction.atomic
def create_technical_slots(event, scenario):
    """Create fixed technical slots in the new event's capture transaction."""
    if scenario not in source.SOURCES or not isinstance(event, source.SOURCES[scenario][1]):
        raise ValueError("review_source_owner_mismatch")
    if not enabled():
        return None
    stay = technical_stay(scenario, event.stay_id)
    comms = source.SOURCES[scenario][4]
    event = type(event).objects.get(pk=event.pk, stay=stay)
    at, now = cutover(), timezone.now()
    if (
        stay.environment != "production"
        or stay.status == "cancelled"
        or not at <= event.created_at <= now < stay.ends_at
        or not source.current_core(stay, event, comms)
        or not payment_eligible(stay, scenario)
        or local_history(stay, scenario) != "not_attempted"
    ):
        return None
    existing = WebsiteReviewCampaign.objects.filter(scenario=scenario, stay_id=stay.pk).first()
    if existing:
        return existing  # Permanent campaign identity; no rescheduling/replacement.
    original, end = schedule(stay.ends_at, stay.time_zone)
    basis = scope(stay, event, scenario)
    campaign = WebsiteReviewCampaign.objects.create(
        scenario=scenario,
        stay_id=stay.pk,
        origin_event_id=event.pk,
        source_occurred_at=event.created_at,
    )
    for step, due in enumerate(original, 1):
        until = original[step] if step < 3 else end
        WebsiteReviewSlot.objects.create(
            campaign=campaign,
            step=step,
            job_sha256=source.sha(f"website-review-slot-v1:{scenario}:{campaign.pk}:{step}"),
            not_before=due,
            expires_at=until,
            basis={"scope": basis, "policy_ref": POLICY_REF},
        )
    return campaign


@transaction.atomic
def register_slot(slot_id):
    """Register one fixed due row for the existing relay worker, never a scanner."""
    slot = WebsiteReviewSlot.objects.select_related("campaign").get(pk=slot_id)
    stay = technical_stay(slot.campaign.scenario, slot.campaign.stay_id)
    slot = WebsiteReviewSlot.objects.select_for_update().select_related("campaign").get(pk=slot_id)
    event = stay.latest_communication_event()
    if (
        not enabled()
        or event is None
        or slot.basis.get("scope") != scope(stay, event, slot.campaign.scenario)
        or timezone.now() >= slot.expires_at
        or slot.not_before < slot.campaign.source_occurred_at
    ):
        return None
    row, _ = WebsiteMessageRelay.objects.get_or_create(
        scenario=slot.campaign.scenario,
        financial=False,
        event_id=slot.pk,
        message_type=f"review_followup_{slot.step}",
        defaults={
            "stay_id": stay.pk,
            "revision": str(stay.communication_revision),
            "job_sha256": slot.job_sha256,
            "source_occurred_at": slot.campaign.source_occurred_at,
            "not_before": slot.not_before,
            "expires_at": slot.expires_at,
        },
    )
    return row


def predecessor_delivered(slot):
    if slot.step == 1:
        return True
    previous = (
        WebsiteReviewSlot.objects.select_related("expectation")
        .filter(campaign=slot.campaign, step=slot.step - 1)
        .first()
    )
    proof = previous.expectation if previous else None
    if not proof or previous.basis != slot.basis:
        return False
    return (
        proof.state in {"delivered", "read"}
        and proof.attempts_claimed == 1
        and bool(proof.provider_message_id and proof.wamid and proof.receipt_evidence_ref)
        and type(proof.observed_at_epoch) is int
        and int(proof.registered_at.timestamp())
        <= proof.observed_at_epoch
        < slot.not_before.timestamp()
        and proof.source_binding
        == {
            "contract": TAG,
            "slot_id": str(previous.pk),
            "campaign_id": str(previous.campaign_id),
            "step": previous.step,
            "basis": previous.basis,
        }
        and proof.identity.get("job_sha256") == previous.job_sha256
        and proof.identity.get("source_event_key")
        == f"website:{slot.campaign.scenario}:review:{previous.pk}"
    )


@sensitive_variables()
def facts_for_row(row):
    scenario = row.scenario
    if scenario not in source.SOURCES or row.financial or row.message_type not in KINDS:
        raise ValueError("review_row_scope_mismatch")
    stay = locked_stay(scenario, row.stay_id)
    slot = (
        WebsiteReviewSlot.objects.select_for_update()
        .select_related("campaign")
        .get(
            pk=row.event_id,
            campaign__stay_id=stay.pk,
            campaign__scenario=scenario,
            step=KINDS[row.message_type],
            job_sha256=row.job_sha256,
        )
    )
    event = stay.latest_communication_event()
    comms = source.SOURCES[scenario][4]
    now = timezone.now()
    if (
        not enabled()
        or event is None
        or not source.current_core(stay, event, comms)
        or slot.campaign.origin_event_id != event.pk
        or slot.campaign.source_occurred_at != event.created_at
        or slot.basis != {"scope": scope(stay, event, scenario), "policy_ref": POLICY_REF}
        or stay.environment != "production"
        or stay.status == "cancelled"
        or not comms.admitted(stay)
        or not payment_eligible(stay, scenario)
        or not cutover() <= slot.campaign.source_occurred_at <= now < slot.expires_at
        or row.revision != str(stay.communication_revision)
        or row.source_occurred_at != slot.campaign.source_occurred_at
        or row.not_before != slot.not_before
        or row.expires_at != slot.expires_at
    ):
        raise ValueError("review_current_source_changed")
    due, end = schedule(stay.ends_at, stay.time_zone)
    if slot.not_before != due[slot.step - 1] or slot.expires_at != (
        due[slot.step] if slot.step < 3 else end
    ):
        raise ValueError("review_schedule_changed")
    facts = source.base_facts(stay, event, scenario, slot.job_sha256, row.message_type)
    legacy = local_history(stay, scenario)
    predecessor = predecessor_delivered(slot)
    facts.update(
        available=True,
        source_family=f"lsa{scenario}_review",
        source_owner_family="website",
        source_event_id=str(slot.pk),
        source_event_key=f"website:{scenario}:review:{slot.pk}",
        source_occurred_at=slot.campaign.source_occurred_at.isoformat(),
        semantic_key=slot.job_sha256,
        semantic_key_version="website-review-slot-v1",
        not_before=slot.not_before.isoformat(),
        expires_at=slot.expires_at.isoformat(),
        review_scope=scope(stay, event, scenario),
        legacy_state=legacy,
        domain_authorized=(
            slot.not_before <= now
            and predecessor
            and legacy == "not_attempted"
            and (not slot.expectation_id or slot.expectation.attempts_claimed == 0)
            and facts["consent"]["revoked"] is not True
        ),
    )
    values = {
        "guest_name": stay.guest_name,
        "listing_name": getattr(stay.property, "name_" + stay.guest_language, "")
        or stay.property.name_en,
        "booking_reference": ("AM-" if scenario == "07" else "DU-") + str(stay.pk)[:8],
        "departure_date": stay.ends_at.astimezone(ZoneInfo(stay.time_zone)).date().isoformat(),
    }
    if not all(isinstance(v, str) and v for v in values.values()):
        raise ValueError("review_local_values_missing")
    facts["template_context"] = {
        k: facts[k]
        for k in (
            "scenario",
            "source_family",
            "job_sha256",
            "reservation_key",
            "reservation_revision",
            "recipient_ref",
            "source_event_key",
            "source_message_type",
            "purpose",
            "language",
            "not_before",
            "expires_at",
        )
    }
    facts["template_context"].update(
        source_context_ref=TAG + ":" + slot.job_sha256,
        recipient_binding_sha256=source.sha(
            facts["reservation_key"] + ":" + facts["recipient_e164"]
        ),
        preflight_status="ready",
        values=values,
        review_campaign_step=slot.step,
    )
    from .message_receipts import projected_expectation

    facts["receipt_expectation"] = projected_expectation(facts)
    facts["blockers"].append("fresh_private_google_policy_and_independent_ack_required")
    return event, source.scoped_facts(facts)


@sensitive_variables()
def read_capability(row, facts):
    return signing.dumps(
        {
            "relay_ref": str(row.pk),
            "slot_id": str(row.event_id),
            "scenario": row.scenario,
            "job_sha256": row.job_sha256,
            "revision": row.revision,
            "recipient_ref": facts["recipient_ref"],
        },
        salt=SALT,
    )


@csrf_exempt
@sensitive_variables()
def authorization(request, *, scenario):
    request._disable_google_integrations = True
    header = "HTTP_X_LSA_" + ("MORNING07" if scenario == "07" else "DAYUSE08") + "_TOKEN"
    capability = request.META.pop(header, "")
    request.__dict__.pop("headers", None)
    if not getattr(settings, "MESSAGE_SOURCE_AUTH_ENABLED", False):
        return source.response({"code": "unavailable"}, 404)
    if request.method != "POST":
        return source.response({"code": "method_not_allowed"}, 405)
    if request.GET or request.content_type != "application/json" or len(request.body) > 1024:
        return source.response({"code": "invalid_request"}, 400)
    try:
        body = json.loads(request.body, object_pairs_hook=source.unique_object)
        if (
            not isinstance(body, dict)
            or set(body) != {"job_sha256"}
            or not isinstance(body["job_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", body["job_sha256"])
        ):
            raise ValueError
    except (ValueError, TypeError):
        return source.response({"code": "invalid_request"}, 400)
    try:
        claims = signing.loads(capability, salt=SALT, max_age=120)
        if (
            not isinstance(claims, dict)
            or set(claims)
            != {"relay_ref", "slot_id", "scenario", "job_sha256", "revision", "recipient_ref"}
            or claims["scenario"] != scenario
            or claims["job_sha256"] != body["job_sha256"]
        ):
            raise ValueError
        with transaction.atomic():
            row = WebsiteMessageRelay.objects.select_for_update().get(
                pk=UUID(claims["relay_ref"]),
                scenario=scenario,
                event_id=UUID(claims["slot_id"]),
                job_sha256=claims["job_sha256"],
                revision=claims["revision"],
                financial=False,
            )
            _, facts = facts_for_row(row)
            if claims["recipient_ref"] != facts["recipient_ref"]:
                raise ValueError
    except Exception:
        return source.response({"code": "unavailable", "dispatch_allowed": False}, 404)
    return source.response(facts)


@sensitive_variables()
def policy_from_source(event, facts, context):
    try:
        row = WebsiteMessageRelay.objects.get(
            scenario=facts["scenario"],
            event_id=UUID(facts["source_event_key"].rsplit(":", 1)[-1]),
            job_sha256=facts["job_sha256"],
            message_type=facts["source_message_type"],
            financial=False,
        )
        _, current = facts_for_row(row)
        if not current["domain_authorized"] or any(
            current.get(k) != facts.get(k)
            for k in (
                "reservation_key",
                "reservation_revision",
                "recipient_ref",
                "recipient_e164",
                "source_event_key",
                "source_message_type",
                "purpose",
                "job_sha256",
                "review_scope",
            )
        ):
            return None
        packet = context["review_policy_snapshot"]
        read_at = parse_datetime(packet["read_at"])
        if (
            read_at is None
            or not timezone.is_aware(read_at)
            or not timedelta(seconds=-5) <= timezone.now() - read_at <= timedelta(seconds=60)
        ):
            return None
        bound = compose_review_context(current, current["template_context"], packet)
        if (
            type(context.get("review_campaign_step")) is not int
            or context != bound
            or set(current["template_context"]["values"]) != LOCAL_VALUES
        ):
            return None
        from .website_template_selector_v2 import match_context, select_source_message

        selected = select_source_message(current, match_context(current, bound))
        return {
            "language": current["language"],
            "message": selected.message,
            "render_policy_ref": selected.selection_ref,
        }
    except Exception:
        return None


def consume_ack(expectation):
    row = expectation.relay
    if row.message_type not in KINDS:
        return
    slot = (
        WebsiteReviewSlot.objects.select_for_update()
        .select_related("campaign")
        .get(pk=row.event_id)
    )
    if (
        slot.expectation_id is not None
        or expectation.attempts_claimed != 0
        or expectation.state != "prepared"
        or expectation.provider_message_id
        or expectation.wamid
    ):
        raise ValueError("review_slot_already_bound")
    _, facts = facts_for_row(row)
    if not facts["domain_authorized"] or any(
        type(expectation.identity.get(key)) is not type(facts[key])
        or expectation.identity[key] != facts[key]
        for key in (
            "scenario",
            "source_family",
            "job_sha256",
            "source_event_key",
            "reservation_key",
            "reservation_revision",
            "recipient_ref",
            "purpose",
            "source_message_type",
        )
    ):
        raise ValueError("review_claim_source_changed")
    expectation.source_binding = {
        "contract": TAG,
        "slot_id": str(slot.pk),
        "campaign_id": str(slot.campaign_id),
        "step": slot.step,
        "basis": slot.basis,
    }
    expectation.save(update_fields=["source_binding"])
    slot.expectation = expectation
    slot.save(update_fields=["expectation"])
