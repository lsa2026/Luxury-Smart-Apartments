"""Source-owned BODY template ACKs. Synthetic stays/proofs only; network forbidden."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.reservations import message_receipts as receipts
from apps.reservations import message_source_authorization as source
from apps.reservations import message_source_relay as relay
from apps.reservations import website_template_policy_v2 as policy
from apps.reservations import website_template_selector_v2 as selector
from apps.reservations import website_text_access_v1 as text_profile
from apps.reservations.message_receipt_models import WebsiteMessageExpectation
from apps.reservations.website_financial_source_v2 import core_variables
from tests import test_message_receipts as old
from tests import test_message_source_authorization as originals
from tests.test_website_template_selector_v2 import CASES

record = originals.record
pytestmark = pytest.mark.django_db
GUIDE_KEYS = [
    "guest_name",
    "listing_name",
    "map_url",
    "property_address",
    "wifi_net",
    "wifi_pass",
    "gate_code",
    "video_url",
]


@pytest.fixture(params=["ar", "en", "fr"])
def text_guide(record, request, settings, monkeypatch):
    scenario, stay, _ = record
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.MESSAGE_SOURCE_V2_ENABLED = True
    settings.MESSAGE_RECEIPTS_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    type(stay.property).objects.filter(pk=stay.property_id).update(
        name_ar="شقة مصطنعة", name_en="Synthetic Apartment", name_fr="Appartement synthétique"
    )
    type(stay).objects.filter(pk=stay.pk).update(guest_language=request.param)
    source.SOURCES[scenario][4].capture_event(stay.pk)
    stay.refresh_from_db()
    event = stay.latest_communication_event()
    settings.MESSAGE_SOURCE_CUTOVER_AT = (event.created_at - timedelta(seconds=1)).isoformat()
    with transaction.atomic():
        relay.register_event(event, scenario)
    rows = relay.WebsiteMessageRelay.objects.filter(event_id=event.pk)
    for index, kind in enumerate(("status_" + event.payload["event_type"], "house_rules"), 1):
        predecessor = rows.get(message_type=kind)
        facts = source.core_facts(stay, event, scenario, predecessor.job_sha256)
        WebsiteMessageExpectation.objects.create(
            request_id="n8n20-" + f"{index:064x}",
            relay=predecessor,
            identity={"recipient_ref": facts["recipient_ref"], "source_message_type": kind},
            prepare_ref=f"{index + 10:064x}",
            prepared_at_epoch=int(timezone.now().timestamp()),
            render_policy_ref="synthetic-predecessor",
            attempts_claimed=1,
            state="delivered",
            provider_message_id=f"synthetic-provider-{index}",
            wamid=f"synthetic-wamid-{index}",
            receipt_evidence_ref=f"synthetic-signed-receipt-{index}",
        )
    row = rows.get(message_type="checkin_guide")
    row.state = "forwarding"
    row.save(update_fields=["state"])
    with transaction.atomic():
        event, facts = relay.current_event(row)
    assert facts["domain_authorized"] is True
    values = {
        **core_variables(event, scenario),
        "property_address": "SYNTHETIC_ADDRESS",
        "map_url": "https://example.invalid/map",
        "video_url": "https://example.invalid/video",
        "gate_code": "SYNTHETIC_GATE",
        "wifi_net": "SYNTHETIC_WIFI",
        "wifi_pass": "SYNTHETIC_PRIVATE_WIFI_PASSWORD",
    }
    context = {
        **{k: facts[k] for k in selector.IDENTITY},
        "source_family": "lsa" + scenario,
        "language": request.param,
        "recipient_ref": facts["recipient_ref"],
        "recipient_binding_sha256": source.sha(
            f"{facts['reservation_key']}:{facts['recipient_e164']}"
        ),
        "source_context_ref": "yc20-template-source-2:synthetic-text-guide",
        "not_before": facts["not_before"],
        "expires_at": facts["expires_at"],
        "values": values,
    }
    selected = policy.policy_from_source(event, facts, context, text_access=True)
    assert selected is not None
    digest = receipts.content_digest(request.param, selected["message"])
    at = int(timezone.now().timestamp()) - 1
    proof = {
        "contract_version": "yc19-receipt-proof-1",
        "source": "backend19",
        "language": request.param,
        "attempt_count_basis": "durable_claim_not_observed_post_count",
        "dispatch_allowed": False,
        "request_id": old.REQUEST_ID,
        "binding_ref": "synthetic-binding",
        **{k: facts[k] for k in selector.IDENTITY},
        "source_family": "lsa" + scenario,
        "recipient_ref": facts["recipient_ref"],
        "content_digest": digest,
        "business_sender": "synthetic-business",
        "waba_id": "2075336595945655",
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
    planned = {
        "contract_version": "yc19-prepared-content-2",
        "request_id": old.REQUEST_ID,
        "prepare_ref": proof["prepare_ref"],
        "prepared_at_epoch": at,
        "language": request.param,
        "message_input": deepcopy(selected["message"]),
        "content_digest": digest,
        "selection_ref": selected["render_policy_ref"],
        "source_pack_sha256": text_profile.selection_identity(selected["message"])[1],
        "content_binding_basis": "planned_pending_independent_source_template_verification",
        "dispatch_allowed": False,
    }
    wrapper = {
        "contract_version": "yc19-render-source-view-2",
        "request_id": old.REQUEST_ID,
        "prepare_ref": proof["prepare_ref"],
        "prepared_at_epoch": at,
        "context": context,
        "source_provenance": "private_runtime_rpc_source20",
        "domain_recheck_required": True,
        "dispatch_allowed": False,
    }

    def fetch(*args, content=False, render_source=False):
        return deepcopy(wrapper if render_source else planned if content else proof)

    monkeypatch.setattr(receipts, "fetch", fetch)
    return row, event, facts, context, proof, planned, wrapper


def test_body_guide_is_independently_rebuilt_and_ack_is_once_only(text_guide, client):
    row, _, _, context, proof, planned, _ = text_guide
    message = planned["message_input"]
    assert message["template_ref"] == "lsa_sameday_checkin_guide_v1"
    assert message["attachment"] is None
    assert message["variables"] == [context["values"][key] for key in GUIDE_KEYS]
    assert set(proof) == receipts.PROOF_FIELDS  # No changes to provider receipt contract.
    for _ in range(2):
        result = client.post(
            reverse("integrations:website_message_receipt"),
            {"request_id": old.REQUEST_ID, "binding_ref": proof["binding_ref"]},
            content_type="application/json",
            headers={receipts.HEADER: "c" * 43},
        )
        assert result.status_code == 200 and result.json() == {
            "code": "prepared",
            "dispatch_allowed": False,
        }
    assert WebsiteMessageExpectation.objects.filter(relay=row).count() == 1
    stored = json.dumps(
        list(WebsiteMessageExpectation.objects.filter(relay=row).values()), default=str
    )
    assert context["values"]["wifi_pass"] not in stored and "c" * 43 not in stored


@pytest.mark.parametrize(
    "mutation",
    ["order", "components", "guest", "attachment", "old_pdf", "claimed", "profile", "profile_sha"],
)
def test_planned_text_or_media_cannot_supply_its_own_authority(text_guide, mutation):
    row, _, _, _, proof, planned, _ = text_guide
    if mutation == "order":
        values = planned["message_input"]["variables"]
        values[2], values[3] = values[3], values[2]
    elif mutation == "components":
        planned["message_input"]["components_sha256"] = "e" * 64
    elif mutation == "guest":
        planned["message_input"]["variables"][0] = "OTHER_SYNTHETIC_GUEST"
    elif mutation == "claimed":
        proof.update(attempts_claimed=1, proof_stage="attempt_claimed", provider_attempted=None)
    elif mutation == "profile":
        planned["selection_ref"] = "yc19-text-access-render-1:" + "e" * 64
    elif mutation == "profile_sha":
        planned["source_pack_sha256"] = "e" * 64
    elif mutation == "old_pdf":
        case = next(
            c
            for c in CASES
            if c["name"] == "lsa_stay_arrival_document_v2" and c["language"] == proof["language"]
        )
        planned["message_input"] = deepcopy(case["expected_message"])
        planned["content_digest"] = proof["content_digest"] = case["expected_digest"]
    else:
        planned["message_input"]["attachment"] = {"kind": "document"}
    try:
        assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic-capability") == "held"
    except ValueError:
        pass
    assert not WebsiteMessageExpectation.objects.filter(relay=row).exists()


def test_only_verified_gate_instruction_replaces_blank_code_without_fabricating_one(text_guide):
    _, event, facts, context, _, _, _ = text_guide
    context["values"].update(gate_code="", gate_access_instruction="SYNTHETIC_NO_EXTERNAL_GATE")
    context["gate_access_evidence_ref"] = "synthetic-owner-gate:20261125"
    selected = policy.policy_from_source(event, facts, context, text_access=True)
    assert selected is not None and selected["message"]["attachment"] is None
    assert selected["message"]["variables"][6] == "SYNTHETIC_NO_EXTERNAL_GATE"
    assert context["values"]["gate_code"] == ""
    del context["gate_access_evidence_ref"]
    assert policy.policy_from_source(event, facts, context, text_access=True) is None


def test_text_template_does_not_waive_predecessor_delivery(text_guide):
    row, _, _, _, _, _, _ = text_guide
    WebsiteMessageExpectation.objects.filter(relay__message_type="house_rules").update(
        state="accepted"
    )
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic-capability") == "held"
    assert not WebsiteMessageExpectation.objects.filter(relay=row).exists()


def test_registered_text_receipt_survives_later_catalog_change_without_new_send(
    text_guide, monkeypatch
):
    row, _, _, _, proof, _, _ = text_guide
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic-capability") == "prepared"
    monkeypatch.setattr(
        selector, "select_source_message", lambda *_: pytest.fail("immutable receipt")
    )
    old.deliver(proof)
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic-capability") == "delivered"
    assert WebsiteMessageExpectation.objects.filter(relay=row).count() == 1


def test_old_pdf_expectation_is_immutable_and_imports_history_without_new_selector(
    text_guide, monkeypatch
):
    row, _, facts, _, proof, _, _ = text_guide
    case = next(
        c
        for c in CASES
        if c["name"] == "lsa_stay_arrival_document_v2" and c["language"] == proof["language"]
    )
    proof["content_digest"] = case["expected_digest"]
    prior = WebsiteMessageExpectation.objects.create(
        request_id=old.REQUEST_ID,
        relay=row,
        identity=receipts.verified_identity(proof, facts, old.REQUEST_ID),
        prepare_ref=proof["prepare_ref"],
        prepared_at_epoch=proof["prepared_at_epoch"],
        render_policy_ref=selector.SELECTION_REF,
    )
    monkeypatch.setattr(
        selector, "select_source_message", lambda *_: pytest.fail("no re-render/adoption")
    )
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic-capability") == "prepared"
    old.deliver(proof)
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic-capability") == "delivered"
    prior.refresh_from_db()
    assert prior.identity["content_digest"] == case["expected_digest"]
    assert WebsiteMessageExpectation.objects.filter(relay=row).count() == 1
