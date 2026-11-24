"""Pure independent validation of the private runtime review policy view.

Copied from backend19's V2 pure validator/composer; no database reader or network
adapter is included. Local domain/step authority remains in Source01's slots.
"""

import copy
import hashlib
import json
import re
from datetime import datetime
from uuid import UUID

VERSION = "yc20-website-review-policy-1"
POLICY_REF = "yc20-website-review-policy-v2:20261124.1"
SCOPE_KEYS = frozenset(
    (
        "scenario",
        "stay_id",
        "reservation_key",
        "reservation_revision",
        "recipient_binding_sha256",
        "listing_map_id",
        "ends_at",
        "time_zone",
        "language",
        "core_source_event_id",
    )
)
WRAPPER_KEYS = frozenset(
    (
        "contract_version",
        "scope",
        "policy",
        "history",
        "source_context_ref",
        "read_at",
        "dispatch_allowed",
    )
)
POLICY_KEYS = frozenset(
    (
        "policy_ref",
        "enabled",
        "checkout_basis",
        "physical_departure_observed",
        "destination_kind",
        "google_profile_id",
        "review_url",
        "verified_destination_ref",
        "property_approval_at",
        "not_before_by_step",
        "expires_at",
    )
)
HISTORY_KEYS = frozenset(
    (
        "paused",
        "pending_owner_confirmation",
        "owner_confirmed_completed",
        "legacy_telegram_jobs",
        "legacy_native_jobs",
        "legacy_core_jobs",
        "historic_delivery_logs",
        "legacy_campaign",
        "local_legacy_state",
        "source01_channel_history_required",
        "guest_step_authority",
    )
)
HASH = re.compile(r"[0-9a-f]{64}")
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _instant(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("invalid_policy_time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("policy_time_requires_zone")
    return parsed


def _scope(value, *, snapshot=False):
    keys = SCOPE_KEYS | {"snapshot_sha256"} if snapshot else SCOPE_KEYS
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid_policy_scope")
    scenario = value["scenario"]
    if scenario not in ("07", "08"):
        raise ValueError("invalid_policy_scenario")
    for key in ("stay_id", "core_source_event_id"):
        if not isinstance(value[key], str) or not UUID_TEXT.fullmatch(value[key]):
            raise ValueError("invalid_source_event_identity")
        UUID(value[key])
    prefix = "morning07:" if scenario == "07" else "dayuse08:"
    if value["reservation_key"] != prefix + value["stay_id"]:
        raise ValueError("invalid_policy_reservation")
    revision = value["reservation_revision"]
    if (
        not isinstance(revision, str)
        or not 1 <= len(revision) <= 128
        or any(ord(c) < 33 or ord(c) > 126 for c in revision)
    ):
        raise ValueError("invalid_policy_revision")
    for key in (
        ("recipient_binding_sha256", "snapshot_sha256")
        if snapshot
        else ("recipient_binding_sha256",)
    ):
        if not isinstance(value[key], str) or not HASH.fullmatch(value[key]):
            raise ValueError("invalid_policy_binding")
    if not isinstance(value["listing_map_id"], str) or not re.fullmatch(
        r"[1-9][0-9]{0,18}", value["listing_map_id"]
    ):
        raise ValueError("invalid_policy_listing")
    if value["time_zone"] not in ("Asia/Riyadh", "Africa/Casablanca") or value["language"] not in (
        "ar",
        "en",
        "fr",
    ):
        raise ValueError("invalid_policy_locale")
    _instant(value["ends_at"])


def _validate(packet, expected):
    if (
        not isinstance(packet, dict)
        or set(packet) != WRAPPER_KEYS
        or packet["contract_version"] != VERSION
        or packet["dispatch_allowed"] is not False
    ):
        raise ValueError("invalid_policy_wrapper")
    actual = packet["scope"]
    _scope(actual, snapshot=True)
    if any(actual[k] != expected[k] for k in SCOPE_KEYS - {"ends_at"}) or _instant(
        actual["ends_at"]
    ) != _instant(expected["ends_at"]):
        raise ValueError("current_policy_scope_changed")
    if not isinstance(packet["source_context_ref"], str) or not re.fullmatch(
        r"website-review-policy:[0-9a-f]{64}", packet["source_context_ref"]
    ):
        raise ValueError("invalid_policy_reference")
    _instant(packet["read_at"])
    policy, history = packet["policy"], packet["history"]
    if (
        not isinstance(policy, dict)
        or set(policy) != POLICY_KEYS
        or policy["policy_ref"] != POLICY_REF
        or type(policy["enabled"]) is not bool
    ):
        raise ValueError("invalid_review_policy")
    if (
        policy["checkout_basis"] != "contractual_ends_at"
        or policy["physical_departure_observed"] is not None
        or policy["property_approval_at"] is not None
        or policy["destination_kind"] != "google"
    ):
        raise ValueError("unsupported_review_basis")
    for key, pattern in (
        ("google_profile_id", r"[0-9]{1,64}"),
        ("review_url", r"https://g[.]page/r/[A-Za-z0-9_-]{1,300}/review"),
        ("verified_destination_ref", r"google-property:[0-9a-f]{64}"),
    ):
        value = policy[key]
        if value is None and not policy["enabled"]:
            continue
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError("invalid_review_destination")
    slots = policy["not_before_by_step"]
    if not isinstance(slots, list) or len(slots) != 3:
        raise ValueError("invalid_review_slots")
    a, b, c = map(_instant, slots)
    if not _instant(actual["ends_at"]) <= a < b < c < _instant(policy["expires_at"]):
        raise ValueError("review_original_schedule_changed")
    if not isinstance(history, dict) or set(history) != HISTORY_KEYS:
        raise ValueError("invalid_review_history")
    for key in (
        "paused",
        "pending_owner_confirmation",
        "owner_confirmed_completed",
        "legacy_campaign",
    ):
        if type(history[key]) is not bool:
            raise ValueError("invalid_review_history_flag")
    counts = (
        "legacy_telegram_jobs",
        "legacy_native_jobs",
        "legacy_core_jobs",
        "historic_delivery_logs",
    )
    if any(type(history[k]) is not int or not 0 <= history[k] < 2**63 for k in counts):
        raise ValueError("invalid_review_history_count")
    local = (
        "no_local_legacy_evidence"
        if not any(history[k] for k in counts) and not history["legacy_campaign"]
        else "legacy_or_uncertain_requires_hold"
    )
    if (
        history["local_legacy_state"] != local
        or history["source01_channel_history_required"] is not True
        or history["guest_step_authority"] != "source01_signed_guest_receipts"
    ):
        raise ValueError("review_history_authority_changed")


REVIEW_FAMILIES = frozenset(("lsa07_review", "lsa08_review"))
REVIEW_PROVENANCE = "authenticated_source01_review+private_runtime_rpc_review_policy20"
LOCAL_VALUES = frozenset(("guest_name", "listing_name", "booking_reference", "departure_date"))
STEP_PURPOSES = {1: "review_request", 2: "review_reminder_2", 3: "review_reminder_3"}


def compose_review_context(facts, context, packet):
    """Only Site01's local four values plus the current authenticated Google URL.

    The packet remains in the temporary0claim source view; not the wire intent.
    This pure function can also be used by Site01's independent content verifier.
    """
    if (
        not isinstance(facts, dict)
        or facts.get("source_owner_family") != "website"
        or facts.get("source_family") not in REVIEW_FAMILIES
    ):
        raise ValueError("authenticated_review_source_required")
    scope = facts.get("review_scope")
    _scope(scope)
    scenario = scope["scenario"]
    if facts["source_family"] != "lsa" + scenario + "_review" or any(
        scope[k] != facts.get(k) for k in ("scenario", "reservation_key", "reservation_revision")
    ):
        raise ValueError("review_domain_scope_changed")
    peer = facts.get("recipient_e164")
    if (
        not isinstance(peer, str)
        or not re.fullmatch(r"\+[1-9][0-9]{7,14}", peer)
        or hashlib.sha256(peer.encode()).hexdigest() != scope["recipient_binding_sha256"]
    ):
        raise ValueError("review_current_recipient_changed")
    if (
        not isinstance(context, dict)
        or context.get("language") != scope["language"]
        or facts.get("language") != scope["language"]
    ):
        raise ValueError("review_current_language_changed")
    _validate(packet, scope)
    policy, history = packet["policy"], packet["history"]
    if (
        not policy["enabled"]
        or any(
            history[k]
            for k in ("paused", "pending_owner_confirmation", "owner_confirmed_completed")
        )
        or history["local_legacy_state"] != "no_local_legacy_evidence"
    ):
        raise ValueError("review_policy_or_history_blocked")
    # Source01 proves its own independent old-channel history and step. A zero
    # local Source20 count does not establish either of them.
    if facts.get("legacy_state") != "not_attempted":
        raise ValueError("review_source01_legacy_unverified")
    step = context.get("review_campaign_step")
    if (
        type(step) is not int
        or step not in STEP_PURPOSES
        or facts.get("purpose") != STEP_PURPOSES[step]
        or facts.get("source_message_type") != "review_followup_" + str(step)
    ):
        raise ValueError("review_source01_step_unverified")
    due = policy["not_before_by_step"][step - 1]
    expiry = policy["not_before_by_step"][step] if step < 3 else policy["expires_at"]
    if _instant(facts.get("not_before")) != _instant(due) or _instant(
        facts.get("expires_at")
    ) != _instant(expiry):
        raise ValueError("review_original_step_window_changed")
    values = context.get("values")
    if not isinstance(values, dict) or set(values) != LOCAL_VALUES:
        raise ValueError("review_source01_local_values_required")
    ref = context.get("source_context_ref")
    if not isinstance(ref, str) or not 1 <= len(ref) <= 256:
        raise ValueError("review_source_context_required")
    result = copy.deepcopy(context)
    result.update(
        source_family=facts["source_family"],
        review_destination_kind="google",
        verified_destination_ref=policy["verified_destination_ref"],
        review_source_context_ref=ref,
        review_policy_snapshot=copy.deepcopy(packet),
        source_context_ref="yc19-website-review-context-1:"
        + hashlib.sha256(
            json.dumps(
                [ref, packet["source_context_ref"]],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    )
    result["values"]["review_url"] = policy["review_url"]
    return result
