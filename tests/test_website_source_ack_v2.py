from copy import deepcopy

import pytest
from django.db import transaction
from django.urls import reverse

from apps.reservations import message_receipts as receipts
from apps.reservations import message_source_relay as relay
from apps.reservations import website_financial_source_v2 as financial
from apps.reservations import website_template_policy_v2 as policy
from apps.reservations import website_template_selector_v2 as selector
from apps.reservations.message_receipt_models import WebsiteMessageExpectation
from tests import test_message_receipts as old

record, journal, prepared = old.record, old.journal, old.prepared
pytestmark = pytest.mark.django_db


def test_initial_binding_ack_uses_existing_forwarding_journal_only(client, prepared, monkeypatch):
    row, _, expected, proof, _ = prepared
    old.synthetic_policy(monkeypatch, expected)
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
    assert WebsiteMessageExpectation.objects.get().relay_id == row.pk


@pytest.mark.parametrize(
    "change", ["binding", "claimed", "bool_claim", "provider", "held", "missing"]
)
def test_initial_binding_cannot_adopt_old_held_or_attempted_state(prepared, change):
    row, _, _, proof, _ = prepared
    if change == "binding":
        proof["binding_ref"] = "other"
    elif change == "claimed":
        proof["attempts_claimed"] = 1
    elif change == "bool_claim":
        proof["attempts_claimed"] = False
    elif change == "provider":
        proof["provider_message_id"] = "already-attempted"
    elif change == "held":
        type(row).objects.filter(pk=row.pk).update(state="held")
    else:
        type(row).objects.filter(pk=row.pk).delete()
    with pytest.raises(ValueError):
        receipts.initial_binding_relay(old.REQUEST_ID, "synthetic-binding", "c" * 43)
    assert not WebsiteMessageExpectation.objects.exists()


@pytest.fixture
def v2(prepared, monkeypatch, settings):
    settings.MESSAGE_SOURCE_V2_ENABLED = True
    row, _, _, proof, _ = prepared
    proof["waba_id"] = "2075336595945655"
    event_model = receipts.source.SOURCES[row.scenario][1]
    event = event_model.objects.select_related("stay__property").get(pk=row.event_id)
    type(event.stay.property).objects.filter(pk=event.stay.property_id).update(
        name_en="Synthetic Apartment"
    )
    with transaction.atomic():
        event, facts = relay.current_event(row)
    context = {
        key: facts[key]
        for key in (
            "scenario",
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
    context.update(
        source_family=f"lsa{row.scenario}",
        source_context_ref="yc20-template-source-2:synthetic",
        recipient_binding_sha256=receipts.source.sha(
            f"{facts['reservation_key']}:{facts['recipient_e164']}"
        ),
        values={
            **financial.variables(event.stay, row.scenario),
            "booking_platform": "Direct Website",
        },
    )
    expected = policy.policy_from_source(event, facts, context)
    assert expected is not None
    digest = receipts.content_digest(expected["language"], expected["message"])
    proof["content_digest"] = digest
    content = dict(
        contract_version="yc19-prepared-content-2",
        request_id=old.REQUEST_ID,
        prepare_ref=proof["prepare_ref"],
        prepared_at_epoch=proof["prepared_at_epoch"],
        language=expected["language"],
        message_input=deepcopy(expected["message"]),
        content_digest=digest,
        selection_ref=selector.SELECTION_REF,
        source_pack_sha256=selector.PACK_SHA,
        content_binding_basis="planned_pending_independent_source_template_verification",
        dispatch_allowed=False,
    )
    wrapper = dict(
        contract_version="yc19-render-source-view-2",
        request_id=old.REQUEST_ID,
        prepare_ref=proof["prepare_ref"],
        prepared_at_epoch=proof["prepared_at_epoch"],
        context=context,
        source_provenance="private_runtime_rpc_source20",
        domain_recheck_required=True,
        dispatch_allowed=False,
    )

    def fetch(*args, content=False, render_source=False):
        return deepcopy(wrapper if render_source else planned if content else proof)

    planned = content
    monkeypatch.setattr(receipts, "fetch", fetch)
    return row, proof, content, wrapper


def test_v2_ack_is_independently_reconstructed_and_historical_receipts_still_import(v2):
    row, proof, _, _ = v2
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic") == "prepared"
    expected = WebsiteMessageExpectation.objects.get()
    assert expected.render_policy_ref == selector.SELECTION_REF
    old.deliver(proof)
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic") == "delivered"
    assert WebsiteMessageExpectation.objects.count() == 1


@pytest.mark.parametrize(
    "change",
    ["guest", "hash", "components", "context_guest", "pack", "provenance", "extra", "waba"],
)
def test_v2_planned_content_cannot_establish_its_own_authority(v2, change):
    row, proof, planned, wrapper = v2
    if change == "guest":
        planned["message_input"]["variables"][0] = "OTHER GUEST"
    elif change == "hash":
        planned["content_digest"] = "e" * 64
    elif change == "components":
        planned["message_input"]["components_sha256"] = "e" * 64
    elif change == "context_guest":
        wrapper["context"]["values"]["guest_name"] = "OTHER GUEST"
    elif change == "pack":
        planned["source_pack_sha256"] = "e" * 64
    elif change == "provenance":
        wrapper["source_provenance"] = "caller_provided"
    elif change == "waba":
        proof["waba_id"] = "unrelated-account"
    else:
        planned["approved"] = True
    try:
        assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic") == "held"
    except ValueError:
        pass
    assert not WebsiteMessageExpectation.objects.exists()
