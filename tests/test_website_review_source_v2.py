"""Synthetic typed models, independent policy/ACK, no external transport."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.reservations import message_receipts as receipts
from apps.reservations import message_source_relay as relay
from apps.reservations import website_review_source_v2 as review
from apps.reservations import website_template_selector_v2 as selector
from apps.reservations.message_receipt_models import WebsiteMessageExpectation
from apps.reservations.review_source_models import WebsiteReviewCampaign, WebsiteReviewSlot
from tests import test_message_receipts as old
from tests import test_message_source_authorization as original

record = original.record
pytestmark = pytest.mark.django_db


@pytest.fixture
def campaign(record, settings, request):
    scenario, stay, _ = original.paid_record(record)
    type(stay.property).objects.filter(pk=stay.property_id).update(
        name_en="Synthetic Apartment", name_fr="Appartement fictif"
    )
    type(stay).objects.filter(pk=stay.pk).update(guest_language=getattr(request, "param", "en"))
    stay.refresh_from_db()
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.MESSAGE_SOURCE_V2_ENABLED = True
    settings.MESSAGE_RECEIPTS_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.MESSAGE_SOURCE_CUTOVER_AT = (timezone.now() - timedelta(hours=1)).isoformat()
    comms = original.source.SOURCES[scenario][4]
    with transaction.atomic():
        comms.capture_event(stay.pk)
    stay.refresh_from_db()
    event = stay.latest_communication_event()
    result = review.create_technical_slots(event, scenario)
    assert result is not None
    return scenario, stay, event, result


def packet(facts, campaign):
    slots = list(campaign.slots.order_by("step"))
    return {
        "contract_version": "yc20-website-review-policy-1",
        "scope": {**facts["review_scope"], "snapshot_sha256": "a" * 64},
        "policy": {
            "policy_ref": review.POLICY_REF,
            "enabled": True,
            "checkout_basis": "contractual_ends_at",
            "physical_departure_observed": None,
            "destination_kind": "google",
            "google_profile_id": "123456789",
            "review_url": "https://g.page/r/synthetic_only/review",
            "verified_destination_ref": "google-property:" + "c" * 64,
            "property_approval_at": None,
            "not_before_by_step": [slot.not_before.isoformat() for slot in slots],
            "expires_at": slots[-1].expires_at.isoformat(),
        },
        "history": {
            "paused": False,
            "pending_owner_confirmation": False,
            "owner_confirmed_completed": False,
            "legacy_telegram_jobs": 0,
            "legacy_native_jobs": 0,
            "legacy_core_jobs": 0,
            "historic_delivery_logs": 0,
            "legacy_campaign": False,
            "local_legacy_state": "no_local_legacy_evidence",
            "source01_channel_history_required": True,
            "guest_step_authority": "source01_signed_guest_receipts",
        },
        "source_context_ref": "website-review-policy:" + "b" * 64,
        "read_at": timezone.now().isoformat(),
        "dispatch_allowed": False,
    }


def due(campaign, monkeypatch, step=1):
    _, _, _, item = campaign
    slot = item.slots.get(step=step)
    row = review.register_slot(slot.pk)
    assert row is not None
    now = slot.not_before + timedelta(seconds=5)
    monkeypatch.setattr(timezone, "now", lambda: now)
    with transaction.atomic():
        _, facts = review.facts_for_row(row)
    return slot, row, facts


@pytest.fixture
def prepared(campaign, monkeypatch):
    slot, row, facts = due(campaign, monkeypatch)
    type(row).objects.filter(pk=row.pk).update(state="forwarding")
    row.refresh_from_db()
    data = packet(facts, campaign[3])
    context = review.compose_review_context(facts, facts["template_context"], data)
    with transaction.atomic():
        expected = review.policy_from_source(campaign[2], facts, context)
    assert expected is not None
    at = int(timezone.now().timestamp())
    proof = {
        **{k: "synthetic" for k in receipts.PROOF_FIELDS},
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
        "contract_version": "yc19-receipt-proof-1",
        "source": "backend19",
        "request_id": old.REQUEST_ID,
        "source_family": facts["source_family"],
        "language": facts["language"],
        "binding_ref": "synthetic-binding",
        "content_digest": receipts.content_digest(expected["language"], expected["message"]),
        "business_sender": "synthetic-sender",
        "waba_id": "2075336595945655",
        "phone_id": "synthetic-phone",
        "transport": "ycloud",
        "proof_stage": "intake_held",
        "attempts_claimed": 0,
        "provider_attempted": False,
        "provider_message_id": None,
        "wamid": None,
        "state": "prepared",
        "delivered": False,
        "dispatch_allowed": False,
        "receipt_evidence_ref": "",
        "observed_at_epoch": None,
        "attempt_count_basis": "durable_claim_not_observed_post_count",
        "prepare_ref": "d" * 64,
        "prepared_at_epoch": at,
    }
    planned = {
        "contract_version": "yc19-prepared-content-2",
        "request_id": old.REQUEST_ID,
        "prepare_ref": proof["prepare_ref"],
        "prepared_at_epoch": at,
        "language": expected["language"],
        "message_input": deepcopy(expected["message"]),
        "content_digest": proof["content_digest"],
        "selection_ref": selector.SELECTION_REF,
        "source_pack_sha256": selector.PACK_SHA,
        "content_binding_basis": "planned_pending_independent_source_template_verification",
        "dispatch_allowed": False,
    }
    wrapper = {
        "contract_version": "yc19-render-source-view-2",
        "request_id": old.REQUEST_ID,
        "prepare_ref": proof["prepare_ref"],
        "prepared_at_epoch": at,
        "context": context,
        "source_provenance": "authenticated_source01_review+private_runtime_rpc_review_policy20",
        "domain_recheck_required": True,
        "dispatch_allowed": False,
    }
    monkeypatch.setattr(
        receipts,
        "fetch",
        lambda *a, content=False, render_source=False: deepcopy(
            wrapper if render_source else planned if content else proof
        ),
    )
    return slot, row, facts, proof, planned, wrapper


def test_technical_creation_does_not_register_or_attempt_and_identity_is_permanent(campaign):
    _, _, event, item = campaign
    assert review.create_technical_slots(event, item.scenario).pk == item.pk
    assert WebsiteReviewCampaign.objects.count() == 1
    assert item.slots.count() == 3
    assert not relay.WebsiteMessageRelay.objects.filter(message_type__in=review.KINDS).exists()
    for slot in item.slots.all():
        assert slot.job_sha256 == review.source.sha(
            f"website-review-slot-v1:{item.scenario}:{item.pk}:{slot.step}"
        )
        assert not slot.expectation_id and "review_url" not in str(slot.basis)
        assert "Synthetic Guest" not in str(slot.basis) and "+16502530000" not in str(slot.basis)


def test_fresh_read_only_job_capability_has_exact_scope_and_no_url(client, campaign, monkeypatch):
    slot, row, facts = due(campaign, monkeypatch)
    cap = review.read_capability(row, facts)
    path = (
        "integrations:"
        + ("morning07" if row.scenario == "07" else "day_use08")
        + "_review_source_authorization"
    )
    with CaptureQueriesContext(connection) as queries:
        result = client.post(
            reverse(path),
            {"job_sha256": slot.job_sha256},
            content_type="application/json",
            headers={
                "X-LSA-" + ("Morning07" if row.scenario == "07" else "DayUse08") + "-Token": cap
            },
        )
    assert result.status_code == 200
    assert all(
        q["sql"].lstrip().split()[0].upper() not in {"INSERT", "UPDATE", "DELETE"} for q in queries
    )
    value = result.json()
    assert value["domain_authorized"] is True and value["dispatch_allowed"] is False
    assert len(value["review_scope"]) == 10
    assert set(value["template_context"]["values"]) == review.LOCAL_VALUES
    assert value["review_scope"]["recipient_binding_sha256"] == review.source.sha(
        campaign[1].guest_phone
    )
    assert (
        value["template_context"]["recipient_binding_sha256"]
        != value["review_scope"]["recipient_binding_sha256"]
    )
    assert "review_url" not in result.content.decode() and cap not in result.content.decode()
    assert "no-store" in result["Cache-Control"] and result["Referrer-Policy"] == "no-referrer"


def test_ack_binds_slot_atomically_and_historical_receipt_imports_after_phone_change(prepared):
    slot, row, _, proof, _, _ = prepared
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic") == "prepared"
    slot.refresh_from_db()
    assert slot.expectation_id == old.REQUEST_ID
    saved = slot.expectation
    assert saved.source_binding["slot_id"] == str(slot.pk)
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic") == "prepared"
    model = original.source.SOURCES[row.scenario][0]
    model.objects.filter(pk=row.stay_id).update(guest_phone="+16502530001", status="cancelled")
    old.deliver(proof)
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic") == "delivered"
    assert WebsiteMessageExpectation.objects.filter(relay=row).count() == 1


@pytest.mark.parametrize(
    "change",
    [
        "guest",
        "url",
        "scope",
        "paused",
        "completed",
        "legacy",
        "step",
        "time",
        "stale_policy",
        "provenance",
        "digest",
        "components",
    ],
)
def test_planned_or_stale_values_cannot_authorize_slot(prepared, change):
    slot, row, _, _, planned, wrapper = prepared
    ctx = wrapper["context"]
    if change == "guest":
        ctx["values"]["guest_name"] = "OTHER"
    elif change == "url":
        ctx["values"]["review_url"] = "https://g.page/r/other/review"
    elif change == "scope":
        ctx["review_policy_snapshot"]["scope"]["stay_id"] = str(uuid4())
    elif change == "paused":
        ctx["review_policy_snapshot"]["history"]["paused"] = True
    elif change == "completed":
        ctx["review_policy_snapshot"]["history"]["owner_confirmed_completed"] = True
    elif change == "legacy":
        ctx["review_policy_snapshot"]["history"]["legacy_native_jobs"] = 1
    elif change == "step":
        ctx["review_campaign_step"] = True
    elif change == "time":
        ctx["not_before"] = timezone.now().isoformat()
    elif change == "stale_policy":
        ctx["review_policy_snapshot"]["read_at"] = (
            timezone.now() - timedelta(minutes=2)
        ).isoformat()
    elif change == "provenance":
        wrapper["source_provenance"] = "caller"
    elif change == "digest":
        planned["content_digest"] = "e" * 64
    elif change == "components":
        planned["message_input"]["components_sha256"] = "e" * 64
    try:
        assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic") == "held"
    except ValueError:
        pass
    slot.refresh_from_db()
    assert (
        slot.expectation_id is None
        and not WebsiteMessageExpectation.objects.filter(relay=row).exists()
    )


@pytest.mark.parametrize("state", ["prepared", "accepted", "handed_off", "delivered", "read"])
def test_next_step_requires_exact_previous_signed_guest_delivery(campaign, monkeypatch, state):
    slot, row, facts = due(campaign, monkeypatch)
    identity = {
        **facts,
        "job_sha256": slot.job_sha256,
        "binding_ref": "synthetic-binding",
        "content_digest": "a" * 64,
    }
    proof = WebsiteMessageExpectation.objects.create(
        request_id="synthetic-prior",
        relay=row,
        identity=identity,
        prepare_ref="e" * 64,
        render_policy_ref="synthetic",
        prepared_at_epoch=int(timezone.now().timestamp()),
    )
    with transaction.atomic():
        review.consume_ack(proof)
    WebsiteMessageExpectation.objects.filter(pk=proof.pk).update(
        state=state,
        attempts_claimed=1 if state != "prepared" else 0,
        provider_message_id="synthetic",
        wamid="synthetic",
        receipt_evidence_ref="synthetic-signed",
        observed_at_epoch=int(timezone.now().timestamp()),
    )
    _, _, later = due(campaign, monkeypatch, 2)
    assert later["domain_authorized"] is (state in {"delivered", "read"})


@pytest.mark.parametrize("change", ["phone", "revision", "end", "unpaid", "cancelled"])
def test_changed_current_source_never_creates_alternate_identity(campaign, monkeypatch, change):
    slot, row, _ = due(campaign, monkeypatch)
    values = {
        "phone": {"guest_phone": "+16502530001"},
        "revision": {"communication_revision": 99},
        "end": {"ends_at": campaign[1].ends_at + timedelta(hours=1)},
        "unpaid": {"paid_at": None},
        "cancelled": {"status": "cancelled"},
    }[change]
    type(campaign[1]).objects.filter(pk=campaign[1].pk).update(**values)
    with transaction.atomic(), pytest.raises(ValueError):
        review.facts_for_row(row)
    assert WebsiteReviewCampaign.objects.count() == 1 and WebsiteReviewSlot.objects.count() == 3
    if change in {"phone", "revision", "end"}:
        assert review.register_slot(slot.pk) is None


@pytest.mark.parametrize("campaign", ["ar", "en", "fr"], indirect=True)
def test_independent_approved_render_uses_each_language_and_four_local_values(prepared):
    _, _, facts, _, planned, wrapper = prepared
    assert planned["language"] == facts["language"]
    assert planned["message_input"]["mode"] == "template"
    assert len(wrapper["context"]["values"]) == 5
    assert wrapper["context"]["values"]["review_url"] == "https://g.page/r/synthetic_only/review"


@pytest.mark.parametrize(
    "bad", ["core_capability", "wrong_scenario", "wrong_job", "extra", "expired"]
)
def test_review_capability_cannot_be_repurposed(client, campaign, monkeypatch, bad):
    import time

    from django.core import signing

    slot, row, facts = due(campaign, monkeypatch)
    if bad == "expired":
        with monkeypatch.context() as clock_patch:
            clock_patch.setattr(
                signing.TimestampSigner,
                "timestamp",
                lambda self: signing.b62_encode(int(time.time()) - 121),
            )
            cap = review.read_capability(row, facts)
    else:
        cap = review.read_capability(row, facts)
    if bad == "core_capability":
        cap = original.source.SOURCES[row.scenario][4].event_token(campaign[2])
    scenario = ("08" if row.scenario == "07" else "07") if bad == "wrong_scenario" else row.scenario
    body = {"job_sha256": "f" * 64 if bad == "wrong_job" else slot.job_sha256}
    if bad == "extra":
        body["purpose"] = "review_request"
    path = (
        "integrations:"
        + ("morning07" if scenario == "07" else "day_use08")
        + "_review_source_authorization"
    )
    result = client.post(
        reverse(path),
        body,
        content_type="application/json",
        headers={"X-LSA-" + ("Morning07" if scenario == "07" else "DayUse08") + "-Token": cap},
    )
    assert result.status_code == (400 if bad == "extra" else 404)
    assert cap not in result.content.decode() and "no-store" in result["Cache-Control"]


@pytest.mark.parametrize(
    "history", ["channel", "notice", "opaque_legacy", "guest_review", "workgroup_review"]
)
def test_old_channel_or_any_uncertain_review_history_holds(campaign, monkeypatch, history):
    from apps.reservations.telegram_stay_models import (
        TelegramStayChannel,
        TelegramStayMessage,
        TelegramStayNotice,
        TelegramWorkgroupHandoff,
    )

    slot, row, _ = due(campaign, monkeypatch)
    scenario, stay = campaign[:2]
    if history == "channel":
        TelegramStayChannel.objects.create(
            scenario=scenario, stay_id=stay.pk, hostaway_id="99007", cutover_at=timezone.now()
        )
    elif history == "notice":
        TelegramStayNotice.objects.create(
            scenario=scenario, stay_id=stay.pk, event_id=uuid4(), revision=1
        )
    elif history == "opaque_legacy":
        original.source.SOURCES[scenario][3].objects.create(
            reference_id="synthetic-legacy", stay=stay, revision=1, payload_hash="a" * 64
        )
    else:
        model = TelegramStayMessage if history == "guest_review" else TelegramWorkgroupHandoff
        model.objects.create(
            scenario=scenario,
            stay_id=stay.pk,
            event_id=uuid4(),
            revision=1,
            purpose="review_request",
            sequence=1,
            job_key="synthetic-old",
            reference_id="synthetic-old",
            identity={},
            booking_basis={},
            state="handed_off",
            not_before=slot.not_before,
            expires_at=slot.expires_at,
        )
    with transaction.atomic():
        _, current = review.facts_for_row(row)
    assert current["domain_authorized"] is False and current["legacy_state"] == "unknown"


def test_late_predecessor_does_not_authorize_compressed_catchup(campaign, monkeypatch):
    slot, row, facts = due(campaign, monkeypatch)
    proof = WebsiteMessageExpectation.objects.create(
        request_id="synthetic-late",
        relay=row,
        identity={**facts, "binding_ref": "synthetic", "content_digest": "a" * 64},
        prepare_ref="f" * 64,
        render_policy_ref="synthetic",
        prepared_at_epoch=int(timezone.now().timestamp()),
    )
    with transaction.atomic():
        review.consume_ack(proof)
    later = campaign[3].slots.get(step=2)
    WebsiteMessageExpectation.objects.filter(pk=proof.pk).update(
        state="delivered",
        attempts_claimed=1,
        provider_message_id="synthetic",
        wamid="synthetic",
        receipt_evidence_ref="synthetic-signed",
        observed_at_epoch=int(later.not_before.timestamp()) + 1,
    )
    _, _, current = due(campaign, monkeypatch, 2)
    assert current["domain_authorized"] is False


def test_expired_slot_cannot_issue_fresh_send_authority(campaign, monkeypatch):
    slot, row, _ = due(campaign, monkeypatch)
    monkeypatch.setattr(timezone, "now", lambda: slot.expires_at)
    with transaction.atomic(), pytest.raises(ValueError):
        review.facts_for_row(row)
    assert review.register_slot(slot.pk) is None


def test_another_request_cannot_adopt_bound_slot(prepared):
    slot, row, _, proof, _, _ = prepared
    assert receipts.reconcile(row.pk, old.REQUEST_ID, "synthetic") == "prepared"
    proof["request_id"] = "n8n20-" + "f" * 64
    try:
        receipts.reconcile(row.pk, proof["request_id"], "synthetic")
        pytest.fail("bound slot must reject another request")
    except (ValueError, receipts.WebsiteMessageExpectation.DoesNotExist):
        pass
    except Exception as exc:
        from django.db import IntegrityError

        assert isinstance(exc, IntegrityError)
    slot.refresh_from_db()
    assert slot.expectation_id == old.REQUEST_ID


@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires real PostgreSQL row locks")
@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_scheduling_keeps_one_campaign_and_three_job_ids(campaign):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    item = campaign[3]
    item.slots.all().delete()
    item.delete()
    barrier = Barrier(2)
    scenario, _, event, _ = campaign

    def run():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            current = original.source.SOURCES[scenario][1].objects.get(pk=event.pk)
            return str(review.create_technical_slots(current, scenario).pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    assert WebsiteReviewCampaign.objects.count() == 1 and WebsiteReviewSlot.objects.count() == 3


@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires real PostgreSQL row locks")
@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_ack_requests_cannot_claim_another_send_identity(prepared, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import IntegrityError, close_old_connections

    slot, row, _, proof, planned, wrapper = prepared
    barrier = Barrier(2)

    def fetch(request_id, _capability, *, content=False, render_source=False):
        result = deepcopy(wrapper if render_source else planned if content else proof)
        result["request_id"] = request_id
        result["prepare_ref"] = review.source.sha(request_id)
        return result

    monkeypatch.setattr(receipts, "fetch", fetch)

    def run(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return receipts.reconcile(row.pk, "n8n20-" + str(index) * 64, "synthetic")
            except (ValueError, IntegrityError):
                return "conflict"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (1, 2)))
    assert sorted(results) == ["conflict", "prepared"]
    slot.refresh_from_db()
    assert slot.expectation_id and WebsiteMessageExpectation.objects.filter(relay=row).count() == 1
