"""Project existing affirmative guest evidence, never resolve consent from it.

Withdrawal coverage is not installed. All operational consent stays unknown.
No backfill, UI, ledger or use of the current phone as historical evidence.
"""

import hashlib
import re

from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime

from apps.core import guest_documents as documents

PURPOSES = [
    "booking_confirmation",
    "booking_change",
    "booking_cancellation",
    "house_rules",
    "checkin_guide",
    "checkout_reminder",
    "payment_confirmation",
]
EXCLUDED = [
    "marketing",
    "loyalty_invitation",
    "review_request",
    "review_reminder_2",
    "review_reminder_3",
]
VERSION = "2026-11-09.1"
SOURCES = {"07": "morning_payment_link", "08": "day_use_payment_link"}
PATHS = {"07": "morning07_checkout", "08": "day_use08_checkout"}


class Incomplete(Exception):
    pass


def aware(value):
    if not isinstance(value, str):
        raise Incomplete("acceptance_time_unverified")
    result = parse_datetime(value)
    if result is None or not timezone.is_aware(result):
        raise Incomplete("acceptance_time_unverified")
    return result


def candidate(stay, scenario, facts, events):
    record = stay.legal_acceptance
    if not isinstance(record, dict) or record.get("source") != SOURCES[scenario]:
        raise Incomplete("affirmative_guest_acceptance_missing")
    if record.get("consents") != {"terms": True, "house_rules": True}:
        raise Incomplete("affirmative_guest_acceptance_missing")
    accepted = aware(record.get("accepted_at"))
    occurred = aware(facts.get("source_occurred_at"))
    if not accepted <= occurred <= timezone.now():
        raise Incomplete("source_event_precedes_acceptance_or_is_future")
    terms = record.get("documents", {}).get("terms", {})
    language = terms.get("language")
    # This is an explicitly audited version, not permission inferred from any
    # newer current text or a matching version label without its actual body.
    if (
        documents.VERSION != VERSION
        or language not in {"ar", "en", "fr"}
        or record.get("documents") != documents.guest_documents(language)
        or record.get("documents_digest") != documents.documents_digest()
    ):
        raise Incomplete("accepted_notice_version_or_text_unverified")
    history = list(events.objects.filter(stay=stay).order_by("revision")[:101])
    if not history or len(history) > 100:
        raise Incomplete("historical_phone_coverage_incomplete")
    if [row.revision for row in history] != list(range(1, stay.communication_revision + 1)):
        raise Incomplete("historical_phone_coverage_incomplete")
    previous, at_acceptance = None, None
    for row in history:
        payload = row.payload
        if (
            previous is not None
            and row.created_at < previous
            or not isinstance(payload, dict)
            or payload.get("source_revision") != row.revision
            or payload.get("source_event_id") != str(row.pk)
            or payload.get("reservation_key") != facts["reservation_key"]
            or not isinstance(payload.get("phone"), str)
            or not re.fullmatch(r"\+[1-9][0-9]{6,14}", payload["phone"])
        ):
            raise Incomplete("historical_phone_coverage_incomplete")
        previous = row.created_at
        if row.created_at <= accepted:
            at_acceptance = row
    if at_acceptance is None:
        raise Incomplete("historical_phone_at_acceptance_missing")
    historical = at_acceptance.payload
    if historical["phone"] != facts["recipient_e164"] or any(
        row.payload["phone"] != historical["phone"]
        for row in history
        if row.revision >= at_acceptance.revision
    ):
        raise Incomplete("recipient_changed_after_acceptance")
    if any(record.get(key) != historical.get(key) for key in ("starts_at", "ends_at")) or (
        record.get("amount_sar") != historical.get("total_price")
    ):
        raise Incomplete("accepted_booking_snapshot_unverified")
    evidence = salted_hmac(
        "website-transaction-permission-evidence.v1",
        f"{facts['reservation_key']}:{record['accepted_at']}:{record['documents_digest']}:"
        f"{at_acceptance.revision}:{historical['phone']}",
        algorithm="sha256",
    ).hexdigest()
    return {
        "contract_version": "lsa01-transaction-permission-evidence-1",
        "evidence_ref": "site01-guest-acceptance:" + evidence,
        "acceptance_path": PATHS[scenario],
        "terms_version": VERSION,
        "terms_language": language,
        "notice_ref": f"LSA-GUEST-TERMS:{VERSION}:{language}:1.1+11.3",
        "accepted_at": record["accepted_at"],
        "accepted_reservation_key": facts["reservation_key"],
        "accepted_recipient_binding_sha256": hashlib.sha256(
            f"{facts['reservation_key']}:{historical['phone']}".encode()
        ).hexdigest(),
        "guest_action_ref": "site01-guest-post:" + evidence,
        "source_record_revision": str(at_acceptance.revision),
    }


def project(stay, scenario, facts, events):
    value = {
        "transaction_permission_evidence": None,
        "transaction_permission_scope": None,
        "transaction_permission_limits": ["withdrawal_authority_not_bound"],
    }
    try:
        if facts.get("purpose") not in PURPOSES:
            raise Incomplete("purpose_outside_transactional_notice")
        evidence = candidate(stay, scenario, facts, events)
        value["transaction_permission_evidence"] = evidence
        value["transaction_permission_scope"] = {
            "business_identity": "Luxury Smart Apartments",
            "reservation_key": facts["reservation_key"],
            "candidate_purposes": list(PURPOSES),
            "effective_from": evidence["accepted_at"],
            "excluded_scopes": list(EXCLUDED),
            "not_retrospective": True,
        }
    except Incomplete as exc:
        value["transaction_permission_limits"].append(str(exc))
    except (ValueError, TypeError, KeyError, AttributeError):
        value["transaction_permission_limits"].append("acceptance_evidence_incomplete")
    return value
