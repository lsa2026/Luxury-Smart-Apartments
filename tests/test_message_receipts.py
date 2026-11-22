"""Synthetic private backend proof, independent renderer and original domain gates."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.reservations import message_receipts as receipts
from apps.reservations import message_source_authorization as source
from apps.reservations import message_source_relay as relay
from apps.reservations.message_receipt_models import WebsiteMessageExpectation
from tests import test_message_source_relay as relay_tests

record = relay_tests.record
journal = relay_tests.journal
pytestmark = pytest.mark.django_db
REQUEST_ID = "n8n20-" + "a" * 64


@pytest.fixture
def prepared(journal, settings, monkeypatch):
    settings.MESSAGE_RECEIPTS_ENABLED = True
    scenario, stay, event = journal
    row = relay.WebsiteMessageRelay.objects.get(message_type="status_new")
    row.state = "forwarding"
    row.save(update_fields=["state"])
    with transaction.atomic():
        _, facts = relay.current_event(row)
    policy = {
        "language": "en",
        "message": {"mode": "text", "text": "Synthetic independent rules"},
        "render_policy_ref": "synthetic-approved-policy-v1",
    }
    digest = receipts.content_digest(policy["language"], policy["message"])
    at = int(timezone.now().timestamp()) - 1
    proof = {
        "contract_version": "yc19-receipt-proof-1",
        "source": "backend19",
        "language": "en",
        "attempt_count_basis": "durable_claim_not_observed_post_count",
        "dispatch_allowed": False,
        "request_id": REQUEST_ID,
        "binding_ref": "synthetic-binding",
        "source_family": f"lsa{scenario}",
        **{
            k: facts[k]
            for k in (
                "scenario",
                "job_sha256",
                "source_event_key",
                "reservation_key",
                "reservation_revision",
                "recipient_ref",
                "purpose",
                "source_message_type",
            )
        },
        "content_digest": digest,
        "business_sender": "synthetic-business",
        "waba_id": "synthetic-waba",
        "phone_id": "synthetic-phone-id",
        "transport": "ycloud",
        "proof_stage": "intake_held",
        "attempts_claimed": 0,
        "provider_attempted": False,
        "provider_message_id": None,
        "wamid": None,
        "state": "prepared",
        "delivered": False,
        "receipt_evidence_ref": "",
        "observed_at_epoch": None,
        "prepare_ref": "b" * 64,
        "prepared_at_epoch": at,
    }
    content = {
        "contract_version": "yc19-prepared-content-1",
        "request_id": REQUEST_ID,
        "prepare_ref": proof["prepare_ref"],
        "prepared_at_epoch": at,
        "language": policy["language"],
        "message_input": policy["message"],
        "content_digest": digest,
        "dispatch_allowed": False,
        "content_binding_basis": "planned_pending_source_render_verification",
    }

    def fetch(_request, _cap, *, content=False, render_source=False):
        if render_source:
            return {
                "contract_version": "yc19-render-source-view-1",
                "request_id": proof["request_id"],
                "prepare_ref": proof["prepare_ref"],
                "prepared_at_epoch": proof["prepared_at_epoch"],
                "context": {},
                "source_provenance": "private_runtime_rpc_source20",
                "domain_recheck_required": True,
                "dispatch_allowed": False,
            }
        return deepcopy(prepared_content if content else proof)

    monkeypatch.setattr(receipts, "fetch", fetch)
    prepared_content = content
    return row, facts, policy, proof, content


def synthetic_policy(monkeypatch, policy):
    # Deliberately isolated test authority, not an operational registry adapter.
    monkeypatch.setattr(receipts, "independent_render_policy", lambda *_: deepcopy(policy))


def deliver(proof):
    proof.update(
        proof_stage="attempt_claimed",
        attempts_claimed=1,
        provider_attempted=True,
        provider_message_id="synthetic-provider-id",
        wamid="synthetic-wamid",
        state="delivered",
        delivered=True,
        receipt_evidence_ref="synthetic-signed-receipt",
        observed_at_epoch=int(timezone.now().timestamp()),
    )


def test_planned_hash_without_independent_renderer_never_creates_expectation(prepared):
    row, _, _, _, _ = prepared
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "held"
    assert not WebsiteMessageExpectation.objects.exists()


def test_pre_attempt_expectation_is_idempotent_and_exports_only_exact_source(prepared, monkeypatch):
    row, facts, policy, _, _ = prepared
    synthetic_policy(monkeypatch, policy)
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "prepared"
    original = WebsiteMessageExpectation.objects.get()
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "prepared"
    assert WebsiteMessageExpectation.objects.count() == 1
    assert receipts.projected_expectation(facts)["expectation_ref"] == str(original.expectation_ref)
    assert receipts.projected_expectation({**facts, "recipient_ref": "other"}) is None
    serialized = json.dumps(list(WebsiteMessageExpectation.objects.values()), default=str)
    assert policy["message"]["text"] not in serialized and "synthetic-capability" not in serialized


@pytest.mark.parametrize(
    "mutation",
    [
        "claimed",
        "recipient",
        "revision_type",
        "kind",
        "event",
        "digest",
        "planned_body",
        "prepare_ref",
        "sender_transport",
    ],
)
def test_invalid_or_late_binding_cannot_adopt_existing_receipt(prepared, monkeypatch, mutation):
    row, _, policy, proof, content = prepared
    synthetic_policy(monkeypatch, policy)
    if mutation == "claimed":
        deliver(proof)
    elif mutation == "recipient":
        proof["recipient_ref"] = "site01:other"
    elif mutation == "revision_type":
        proof["reservation_revision"] = 1
    elif mutation == "kind":
        proof["source_message_type"] = "payment_success"
    elif mutation == "event":
        proof["source_event_key"] = "website:07:core:wrong"
    elif mutation == "digest":
        proof["content_digest"] = "c" * 64
    elif mutation == "planned_body":
        content["message_input"] = {"mode": "text", "text": "Changed caller body"}
    elif mutation == "prepare_ref":
        proof["prepare_ref"] = None
    else:
        proof["transport"] = "ultramsg"
    try:
        assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "held"
    except ValueError:
        pass
    assert not WebsiteMessageExpectation.objects.exists()


def test_accepted_is_not_delivery_unknown_blocks_and_actual_receipt_matches_expectation(
    prepared, monkeypatch
):
    row, facts, policy, proof, _ = prepared
    synthetic_policy(monkeypatch, policy)
    receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability")
    proof.update(
        proof_stage="attempt_claimed",
        attempts_claimed=1,
        provider_attempted=None,
        state="uncertain",
    )
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "uncertain"
    stay = source.SOURCES[row.scenario][0].objects.get(pk=row.stay_id)
    assert source.core_history(stay, row.message_type, row.scenario) == "uncertain"
    proof.update(
        provider_message_id="synthetic-provider-id",
        wamid="synthetic-wamid",
        provider_attempted=True,
        state="accepted",
    )
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "accepted"
    assert not receipts.core_delivered(stay, int(row.revision), row.scenario)
    deliver(proof)
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "delivered"
    assert not receipts.core_delivered(stay, int(row.revision), row.scenario)  # Other two absent.
    assert receipts.projected_expectation(facts)["registered_before_attempt"] is True
    proof.update(state="accepted", delivered=False)
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "delivered"


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_digest", "c" * 64),
        ("provider_message_id", "another"),
        ("business_sender", "other-business"),
        ("wamid", "another"),
        ("receipt_evidence_ref", None),
        ("observed_at_epoch", 1),
        ("provider_attempted", None),
    ],
)
def test_receipt_correlations_cannot_change_or_claim_delivery_without_evidence(
    prepared, monkeypatch, field, value
):
    row, _, policy, proof, _ = prepared
    synthetic_policy(monkeypatch, policy)
    receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability")
    deliver(proof)
    receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability")
    proof[field] = value
    with pytest.raises(ValueError):
        receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability")
    assert WebsiteMessageExpectation.objects.get().state == "delivered"


def test_endpoint_accepts_no_read_capability_cookie_or_caller_delivery_flag(client, prepared):
    row, _, _, _, _ = prepared
    url = reverse("integrations:website_message_receipt")
    for headers, body in [
        (
            {"HTTP_X_LSA_SOURCE_READ_CAPABILITY": "not-receipt-authority"},
            {"request_id": REQUEST_ID, "relay_ref": str(row.pk)},
        ),
        (
            {"HTTP_X_LSA_RECEIPT_CAPABILITY": "x" * 43},
            {"request_id": REQUEST_ID, "relay_ref": str(row.pk), "delivered": True},
        ),
    ]:
        result = client.post(url, data=json.dumps(body), content_type="application/json", **headers)
        assert result.status_code == 409
        assert result["Cache-Control"] == "private, no-store, max-age=0"
    assert not WebsiteMessageExpectation.objects.exists()


def test_all_three_verified_receipts_satisfy_original_gate_and_phone_change_revokes_it(
    prepared, monkeypatch
):
    first, _, policy, proof, content = prepared
    synthetic_policy(monkeypatch, policy)
    stay = (
        source.SOURCES[first.scenario][0].objects.select_related("property").get(pk=first.stay_id)
    )
    rows = [
        relay.WebsiteMessageRelay.objects.get(message_type=kind)
        for kind in ("status_new", "house_rules", "checkin_guide")
    ]
    for sequence, row in enumerate(rows, 1):
        row.state = "forwarding"
        row.save(update_fields=["state"])
        with transaction.atomic():
            _, facts = relay.current_event(row)
        request_id = "n8n20-" + f"{sequence:064x}"
        prepare_ref = f"{sequence + 10:064x}"
        proof.update(
            {
                k: facts[k]
                for k in (
                    "job_sha256",
                    "source_event_key",
                    "reservation_key",
                    "reservation_revision",
                    "recipient_ref",
                    "purpose",
                    "source_message_type",
                )
            }
        )
        proof.update(
            request_id=request_id,
            prepare_ref=prepare_ref,
            proof_stage="intake_held",
            attempts_claimed=0,
            provider_attempted=False,
            provider_message_id=None,
            wamid=None,
            state="prepared",
            delivered=False,
            receipt_evidence_ref="",
            observed_at_epoch=None,
        )
        content.update(request_id=request_id, prepare_ref=prepare_ref)
        assert receipts.reconcile(row.pk, request_id, "synthetic-capability") == "prepared"
        deliver(proof)
        assert receipts.reconcile(row.pk, request_id, "synthetic-capability") == "delivered"
    assert receipts.core_delivered(stay, stay.communication_revision, first.scenario)
    if first.scenario == "07":
        from apps.reservations.morning_loyalty import core_delivered
    else:
        from apps.reservations.day_use_loyalty import core_delivered
    assert core_delivered(stay, stay.communication_revision)
    stay.guest_phone = "+442079460000"
    assert not core_delivered(stay, stay.communication_revision)


@pytest.mark.parametrize("mutation", ["expired", "wrong-request", "extra", "language", "acktrue"])
def test_proof_schema_and_prepare_lifetime_fail_closed(prepared, monkeypatch, mutation):
    row, _, policy, proof, _ = prepared
    synthetic_policy(monkeypatch, policy)
    if mutation == "expired":
        proof["prepared_at_epoch"] -= 301
    elif mutation == "wrong-request":
        proof["request_id"] = "n8n20-" + "d" * 64
    elif mutation == "extra":
        proof["caller_delivered"] = True
    elif mutation == "language":
        proof["language"] = "ar"
    else:
        proof["dispatch_allowed"] = True
    with pytest.raises(ValueError):
        receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability")
    assert not WebsiteMessageExpectation.objects.exists()


def test_late_delivery_callback_uses_existing_expectation_without_read_or_resend(
    client, prepared, monkeypatch
):
    row, _, policy, proof, _ = prepared
    synthetic_policy(monkeypatch, policy)
    receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability")
    prior = WebsiteMessageExpectation.objects.get()
    deliver(proof)
    url = reverse("integrations:website_message_receipt")
    body = {"request_id": REQUEST_ID, "expectation_ref": str(prior.expectation_ref)}
    for _ in range(2):
        reply = client.post(
            url,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_LSA_RECEIPT_CAPABILITY="x" * 43,
        )
        assert reply.status_code == 200 and reply.json()["code"] == "delivered"
    assert WebsiteMessageExpectation.objects.count() == 1
    assert WebsiteMessageExpectation.objects.get().state == "delivered"
    wrong = client.post(
        url,
        data=json.dumps({**body, "expectation_ref": str(uuid4())}),
        content_type="application/json",
        HTTP_X_LSA_RECEIPT_CAPABILITY="x" * 43,
    )
    assert wrong.status_code == 409


def test_callback_reference_cannot_create_first_expectation(client, prepared):
    _, _, _, proof, _ = prepared
    deliver(proof)
    reply = client.post(
        reverse("integrations:website_message_receipt"),
        data=json.dumps({"request_id": REQUEST_ID, "expectation_ref": str(uuid4())}),
        content_type="application/json",
        HTTP_X_LSA_RECEIPT_CAPABILITY="x" * 43,
    )
    assert reply.status_code == 409 and not WebsiteMessageExpectation.objects.exists()


def test_read_receipt_advances_delivery_and_cannot_be_downgraded(prepared, monkeypatch):
    row, _, policy, proof, _ = prepared
    synthetic_policy(monkeypatch, policy)
    receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability")
    deliver(proof)
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "delivered"
    proof["state"] = "read"
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "read"
    proof.update(state="accepted", delivered=False)
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "read"


@pytest.mark.parametrize("change", ["paused", "cutover", "ended", "recipient", "revision"])
def test_existing_expectation_import_survives_future_send_changes(
    prepared, monkeypatch, settings, client, change
):
    from datetime import timedelta

    row, _, policy, proof, _ = prepared
    synthetic_policy(monkeypatch, policy)
    receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability")
    expectation = WebsiteMessageExpectation.objects.get()
    stay_model = source.SOURCES[row.scenario][0]
    if change == "paused":
        settings.MESSAGE_SOURCE_RELAY_ENABLED = False
    elif change == "cutover":
        settings.MESSAGE_SOURCE_CUTOVER_AT = timezone.now() + timedelta(days=1)
    elif change == "ended":
        stay_model.objects.filter(pk=row.stay_id).update(
            ends_at=timezone.now() - timedelta(hours=1)
        )
    elif change == "recipient":
        stay_model.objects.filter(pk=row.stay_id).update(guest_phone="+12025550999")
    else:
        stay_model.objects.filter(pk=row.stay_id).update(communication_revision=999)
    monkeypatch.setattr(relay, "current_event", lambda *_: pytest.fail("No new send authorization"))
    deliver(proof)
    result = client.post(
        reverse("integrations:website_message_receipt"),
        {"request_id": REQUEST_ID, "expectation_ref": str(expectation.expectation_ref)},
        content_type="application/json",
        HTTP_X_LSA_RECEIPT_CAPABILITY="x" * 43,
    )
    assert result.status_code == 200 and result.json()["code"] == "delivered"
    assert result.json()["dispatch_allowed"] is False
    assert WebsiteMessageExpectation.objects.count() == 1


def test_paused_relay_cannot_create_first_expectation(prepared, settings, monkeypatch):
    row, _, policy, _, _ = prepared
    synthetic_policy(monkeypatch, policy)
    settings.MESSAGE_SOURCE_RELAY_ENABLED = False
    assert receipts.reconcile(row.pk, REQUEST_ID, "synthetic-capability") == "held"
    assert not WebsiteMessageExpectation.objects.exists()
