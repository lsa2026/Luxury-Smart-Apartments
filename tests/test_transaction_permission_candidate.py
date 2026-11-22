"""Evidence candidates are not permission grants; all inputs are synthetic."""

from copy import deepcopy
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core import guest_documents as documents
from apps.reservations import message_source_authorization as source
from apps.reservations import transaction_permission_candidate as candidate
from tests import test_message_source_authorization as source_tests

pytestmark = pytest.mark.django_db
record = source_tests.record


@pytest.fixture
def accepted(record):
    scenario, stay, event = record
    now = timezone.now()
    model = source.SOURCES[scenario][1]
    model.objects.filter(pk=event.pk).update(created_at=now - timedelta(seconds=3))
    stay.legal_acceptance = {
        "source": candidate.SOURCES[scenario],
        "documents": documents.guest_documents("en"),
        "documents_digest": documents.documents_digest(),
        "consents": {"terms": True, "house_rules": True},
        "accepted_at": (now - timedelta(seconds=2)).isoformat(),
        "starts_at": event.payload["starts_at"],
        "ends_at": event.payload["ends_at"],
        "amount_sar": event.payload["total_price"],
    }
    facts = {
        "reservation_key": event.payload["reservation_key"],
        "recipient_e164": event.payload["phone"],
        "purpose": "payment_confirmation",
        "source_occurred_at": (now - timedelta(seconds=1)).isoformat(),
    }
    return scenario, stay, model, facts


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_verified_existing_snapshot_exports_candidate_only_without_mutation(accepted, language):
    scenario, stay, events, facts = accepted
    stay.legal_acceptance["documents"] = documents.guest_documents(language)
    original = deepcopy(stay.legal_acceptance)
    result = candidate.project(stay, scenario, facts, events)
    evidence = result["transaction_permission_evidence"]
    assert evidence["terms_language"] == language
    assert evidence["source_record_revision"] == "1"
    assert evidence["accepted_at"] == original["accepted_at"]
    assert evidence["accepted_reservation_key"] == facts["reservation_key"]
    assert evidence["acceptance_path"] == candidate.PATHS[scenario]
    assert result["transaction_permission_limits"] == ["withdrawal_authority_not_bound"]
    assert result["transaction_permission_scope"]["not_retrospective"] is True
    assert "loyalty_invitation" not in result["transaction_permission_scope"]["candidate_purposes"]
    assert "consent" not in result and facts["recipient_e164"] not in str(result)
    assert stay.legal_acceptance == original
    assert source.projected_consent(stay, scenario, facts["purpose"])["revoked"] is None
    assert source.projected_consent(stay, scenario, facts["purpose"])["service"] is None


@pytest.mark.parametrize(
    "case",
    [
        "admin",
        "offline",
        "missing",
        "unchecked",
        "old_version",
        "tampered_text",
        "missing_phone_history",
        "noncanonical_phone",
        "current_phone_changed",
        "amount",
        "naive_time",
        "future_time",
        "earlier_message",
        "marketing",
        "loyalty",
        "wrong_key",
    ],
)
def test_missing_or_out_of_scope_evidence_stays_unknown(accepted, case):
    scenario, stay, events, facts = accepted
    record = stay.legal_acceptance
    if case in {"admin", "offline"}:
        record["source"] = case
    elif case == "missing":
        stay.legal_acceptance = {}
    elif case == "unchecked":
        record["consents"]["terms"] = False
    elif case == "old_version":
        record["documents"]["terms"]["version"] = "old"
    elif case == "tampered_text":
        record["documents"]["terms"]["body"] += "changed"
    elif case == "missing_phone_history":
        events.objects.filter(stay=stay).delete()
    elif case == "noncanonical_phone":
        row = events.objects.get(stay=stay)
        row.payload["phone"] = "0500000000"
        row.save(update_fields=["payload"])
    elif case == "current_phone_changed":
        facts["recipient_e164"] = "+442079460000"
    elif case == "amount":
        record["amount_sar"] = "1.00"
    elif case == "naive_time":
        record["accepted_at"] = "2026-11-22T12:00:00"
    elif case == "future_time":
        record["accepted_at"] = (timezone.now() + timedelta(minutes=1)).isoformat()
    elif case == "earlier_message":
        facts["source_occurred_at"] = (timezone.now() - timedelta(minutes=1)).isoformat()
    elif case in {"marketing", "loyalty"}:
        facts["purpose"] = "marketing" if case == "marketing" else "loyalty_invitation"
    else:
        facts["reservation_key"] = "different-booking"
    result = candidate.project(stay, scenario, facts, events)
    assert result["transaction_permission_evidence"] is None
    assert result["transaction_permission_scope"] is None
    assert "withdrawal_authority_not_bound" in result["transaction_permission_limits"]
    assert len(result["transaction_permission_limits"]) == 2


def test_phone_change_then_change_back_does_not_restore_candidate(accepted):
    scenario, stay, events, facts = accepted
    previous = events.objects.get(stay=stay)
    now = timezone.now()
    for revision, phone in ((2, "+442079460000"), (3, facts["recipient_e164"])):
        row = events(stay=stay, revision=revision)
        row.payload = {
            **previous.payload,
            "source_revision": revision,
            "source_event_id": str(row.pk),
            "phone": phone,
        }
        row.save()
        events.objects.filter(pk=row.pk).update(
            created_at=now - timedelta(microseconds=4 - revision)
        )
    stay.communication_revision = 3
    result = candidate.project(stay, scenario, facts, events)
    assert result["transaction_permission_evidence"] is None
    assert "recipient_changed_after_acceptance" in result["transaction_permission_limits"]
