"""Synthetic Django source/ACK/registry integration; no guest, provider or credit mutation."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.loyalty import invitation_api
from apps.loyalty import invitation_registry as registry
from apps.loyalty.models import JoinInvitation, Ledger, Member
from apps.reservations import message_receipts as receipts
from apps.reservations import message_source_relay as relay
from apps.reservations import website_loyalty_source_v2 as loyalty
from apps.reservations import website_template_policy_v2 as policy
from apps.reservations import website_template_selector_v2 as selector
from apps.reservations.message_receipt_models import WebsiteMessageExpectation
from tests import test_message_receipts as old_receipts
from tests import test_message_source_authorization as original

record = original.record
pytestmark = pytest.mark.django_db


def expectation(row, facts):
    token = uuid4().hex
    return WebsiteMessageExpectation.objects.create(
        request_id="n8n20-" + token * 2,
        relay=row,
        prepare_ref=token * 2,
        render_policy_ref="synthetic-approved-source",
        prepared_at_epoch=int(timezone.now().timestamp()) - 1,
        state="delivered",
        attempts_claimed=1,
        provider_message_id="synthetic-" + token,
        wamid="synthetic-wamid-" + token,
        receipt_evidence_ref="synthetic-signed-" + token,
        identity={
            **facts,
            "source_family": f"lsa{row.scenario}" + ("_payment" if row.financial else ""),
            "binding_ref": "synthetic-binding-" + token,
            "content_digest": "a" * 64,
            "business_sender": "synthetic-sender",
            "waba_id": "2075336595945655",
            "phone_id": "synthetic-phone",
            "transport": "ycloud",
        },
    )


@pytest.fixture
def paid(record, settings, monkeypatch):
    scenario, stay, _ = record
    type(stay.property).objects.filter(pk=stay.property_id).update(
        name_en="Synthetic Apartment", name_ar="شقة مصطنعة", name_fr="Appartement fictif"
    )
    settings.MESSAGE_SOURCE_RELAY_ENABLED = False
    from apps.reservations.models import DayUseOfflineReceipt, MorningOfflineReceipt

    at = timezone.now()
    receipt = MorningOfflineReceipt if scenario == "07" else DayUseOfflineReceipt
    receipt.objects.create(
        stay=stay,
        amount=stay.amount_sar,
        method="cash",
        received_at=at,
        recorded_by=stay.created_by,
    )
    type(stay).objects.filter(pk=stay.pk).update(status="paid", paid_at=at)
    stay.refresh_from_db()
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.MESSAGE_SOURCE_V2_ENABLED = True
    settings.MESSAGE_RECEIPTS_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.MESSAGE_SOURCE_CUTOVER_AT = (stay.paid_at - timedelta(seconds=5)).isoformat()
    settings.MESSAGE_LOYALTY_SOURCE_V2_ENABLED = True
    settings.MESSAGE_LOYALTY_SOURCE_CUTOVER_AT = settings.MESSAGE_SOURCE_CUTOVER_AT
    monkeypatch.setattr(loyalty, "ready", lambda: True)
    comms = original.source.SOURCES[scenario][4]
    with transaction.atomic():
        comms.capture_event(stay.pk)
    stay.refresh_from_db()
    core = stay.latest_communication_event()
    with transaction.atomic():
        original.source.SOURCES[scenario][5].capture_payment(stay.pk)
    event = stay.payment_events.order_by("-created_at").first()
    with transaction.atomic():
        relay.register_event(core, scenario)
        relay.register_event(event, scenario, financial=True)
    for row in relay.WebsiteMessageRelay.objects.filter(scenario=scenario, financial=False).exclude(
        message_type="reminder_checkout"
    ):
        facts = original.source.base_facts(stay, core, scenario, row.job_sha256, row.message_type)
        expectation(row, facts)
    row = relay.WebsiteMessageRelay.objects.get(
        scenario=scenario, financial=True, message_type="payment_success"
    )
    facts = original.source.base_facts(
        stay, event, scenario, row.job_sha256, "payment_success", financial=True
    )
    expectation(row, facts)
    row = relay.WebsiteMessageRelay.objects.get(
        scenario=scenario, financial=True, message_type="loyalty_invitation"
    )
    return scenario, stay, event, row


def read(paid):
    return loyalty.source(paid[2].pk, paid[0])


def claim(paid):
    with loyalty.source_lock():
        loyalty.prepare_for_relay(paid[3])
    return JoinInvitation.objects.get()


@pytest.fixture
def prepared(paid, settings, monkeypatch):
    invitation = claim(paid)
    scenario, stay, event, row = paid
    row.state = "forwarding"
    row.save(update_fields=["state"])
    facts = read(paid)
    assert facts.get("domain_authorized") is True, facts.get("reason", facts.get("blockers"))
    context = {**facts["template_context"], "source_family": f"lsa{scenario}_payment"}
    selected = policy.policy_from_source(event, facts, context)
    assert selected is not None
    request_id = "n8n20-" + "b" * 64
    now = int(timezone.now().timestamp()) - 1
    proof = {
        "contract_version": "yc19-receipt-proof-1",
        "source": "backend19",
        "language": facts["language"],
        "dispatch_allowed": False,
        "attempt_count_basis": "durable_claim_not_observed_post_count",
        "request_id": request_id,
        "binding_ref": "synthetic-loyalty-binding",
        "source_family": f"lsa{scenario}_payment",
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
        "content_digest": receipts.content_digest(selected["language"], selected["message"]),
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
        "receipt_evidence_ref": "",
        "observed_at_epoch": None,
        "prepare_ref": "e" * 64,
        "prepared_at_epoch": now,
    }
    planned = {
        "contract_version": "yc19-prepared-content-2",
        "request_id": request_id,
        "prepare_ref": proof["prepare_ref"],
        "prepared_at_epoch": now,
        "language": selected["language"],
        "message_input": selected["message"],
        "content_digest": proof["content_digest"],
        "selection_ref": selector.SELECTION_REF,
        "source_pack_sha256": selector.PACK_SHA,
        "content_binding_basis": "planned_pending_independent_source_template_verification",
        "dispatch_allowed": False,
    }
    wrapper = {
        "contract_version": "yc19-render-source-view-2",
        "request_id": request_id,
        "prepare_ref": proof["prepare_ref"],
        "prepared_at_epoch": now,
        "context": context,
        "source_provenance": "authenticated_source01_financial",
        "domain_recheck_required": True,
        "dispatch_allowed": False,
    }

    def fetch(*a, content=False, render_source=False):
        return deepcopy(wrapper if render_source else planned if content else proof)

    monkeypatch.setattr(receipts, "fetch", fetch)
    return paid, invitation, proof, planned, wrapper


def test_read_cannot_claim_or_consume_and_source_claim_retains_original_identity(paid):
    with CaptureQueriesContext(connection) as queries:
        before = read(paid)
    assert before["loyalty_source_eligible"] is True and before["domain_authorized"] is False
    assert not JoinInvitation.objects.exists()
    assert not any(
        q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries
    )
    invitation = claim(paid)
    facts = read(paid)
    assert facts["domain_authorized"] is True and facts["dispatch_allowed"] is False
    assert facts["loyalty_identity"]["loyalty_url_expires_at"] is None
    assert facts["variables"]["loyalty_url"] == "https://example.invalid/my-bookings/rewards/"
    assert invitation.channel == "ycloud" and invitation.origin == "website"
    assert invitation.dispatched_at is None
    assert claim(paid).pk == invitation.pk
    snapshot = invitation_api.snapshot(invitation, allow=True)
    assert snapshot["channel"] == "ycloud" and snapshot["provider_receipt_lookup"] is False
    assert snapshot["send_allowed"] is False


def test_ack_atomically_consumes_one_source_permit_and_binds_request(prepared):
    paid, invitation, proof, _, _ = prepared
    row = paid[3]
    assert receipts.reconcile(row.pk, proof["request_id"], "synthetic") == "prepared"
    stored = WebsiteMessageExpectation.objects.get(request_id=proof["request_id"])
    invitation.refresh_from_db()
    assert invitation.state == "unknown" and invitation.dispatched_at
    assert stored.attempts_claimed == 0
    assert stored.source_binding == loyalty.binding(invitation, stored)
    assert stored.source_binding["provider_reference"] == invitation.provider_reference
    assert "source_binding" not in receipts.expectation_snapshot(stored)
    assert receipts.reconcile(row.pk, proof["request_id"], "synthetic") == "prepared"
    assert read(paid)["domain_authorized"] is True
    assert not Member.objects.exists() and not Ledger.objects.exists()


@pytest.mark.parametrize("change", ["url", "registry", "provenance", "recipient", "claimed"])
def test_mismatched_ack_never_consumes_the_registry(prepared, change):
    paid, invitation, proof, planned, wrapper = prepared
    if change == "url":
        wrapper["context"]["values"]["loyalty_url"] = "https://example.invalid/other"
    elif change == "registry":
        wrapper["context"]["loyalty_identity"]["generation"] += 1
    elif change == "provenance":
        wrapper["source_provenance"] = "private_runtime_rpc_source20"
    elif change == "recipient":
        proof["recipient_ref"] = "source20:wrong"
    else:
        old_receipts.deliver(proof)
    try:
        assert receipts.reconcile(paid[3].pk, proof["request_id"], "synthetic") == "held"
    except ValueError:
        pass
    invitation.refresh_from_db()
    assert invitation.state == "claimed" and invitation.dispatched_at is None
    assert not WebsiteMessageExpectation.objects.filter(request_id=proof["request_id"]).exists()


@pytest.mark.parametrize(
    "change",
    ["phone", "revision", "paid", "notice", "programme", "cancel", "old", "channel", "member"],
)
def test_real_domain_or_identity_failure_keeps_invitation_closed(
    paid, settings, monkeypatch, change
):
    scenario, stay, event, row = paid
    invitation = claim(paid)
    if change == "phone":
        type(stay).objects.filter(pk=stay.pk).update(guest_phone="+16502539999")
    elif change == "revision":
        type(stay).objects.filter(pk=stay.pk).update(
            communication_revision=stay.communication_revision + 1
        )
    elif change == "paid":
        type(stay).objects.filter(pk=stay.pk).update(amount_sar=stay.amount_sar + 1)
    elif change == "notice":
        WebsiteMessageExpectation.objects.filter(relay__message_type="payment_success").update(
            state="accepted"
        )
    elif change == "programme":
        monkeypatch.setattr(loyalty, "ready", lambda: False)
    elif change == "cancel":
        type(stay).objects.filter(pk=stay.pk).update(status="cancelled")
    elif change == "old":
        settings.MESSAGE_LOYALTY_SOURCE_CUTOVER_AT = (
            event.created_at + timedelta(seconds=1)
        ).isoformat()
    elif change == "channel":
        invitation.channel = "workgroup_manual"
        invitation.save(update_fields=["channel"])
    else:
        user = get_user_model().objects.create_user(username="synthetic-current-member")
        VerifiedPhone.objects.create(user=user, phone=stay.guest_phone)
        Member.objects.create(
            customer=user,
            number="TEST-MEMBER",
            consent_at=timezone.now(),
            terms_version="synthetic",
            terms_reference="https://example.invalid/terms",
            language="en",
        )
    assert read(paid).get("domain_authorized") is not True
    with loyalty.source_lock():
        loyalty.prepare_for_relay(row)
    assert JoinInvitation.objects.count() == 1
    invitation.refresh_from_db()
    assert invitation.dispatched_at is None


def test_historical_receipt_uses_stored_binding_not_new_phone_or_window(prepared):
    paid, invitation, proof, _, _ = prepared
    row = paid[3]
    assert receipts.reconcile(row.pk, proof["request_id"], "synthetic") == "prepared"
    old_receipts.deliver(proof)
    proof["provider_message_id"] = "p" * 512
    type(paid[1]).objects.filter(pk=paid[1].pk).update(
        guest_phone="+16502539999",
        status="cancelled",
        starts_at=timezone.now() - timedelta(hours=2),
        ends_at=timezone.now() - timedelta(seconds=1),
    )
    assert receipts.reconcile(row.pk, proof["request_id"], "synthetic") == "delivered"
    invitation.refresh_from_db()
    assert (
        invitation.state == "delivered" and invitation.provider_id == proof["provider_message_id"]
    )
    assert receipts.reconcile(row.pk, proof["request_id"], "synthetic") == "delivered"
    assert JoinInvitation.objects.count() == 1 and not Member.objects.exists()


def test_ambiguous_consumed_permit_cannot_be_renewed_or_used_by_public_flow(prepared):
    paid, invitation, proof, _, _ = prepared
    assert receipts.reconcile(paid[3].pk, proof["request_id"], "synthetic") == "prepared"
    invitation.refresh_from_db()
    with pytest.raises(ValidationError, match="website_invitation_permit_required"):
        registry.consume(invitation, phone=invitation.phone, morning_stay=paid[1])
    before = (invitation.pk, invitation.generation, invitation.dispatched_at)
    with loyalty.source_lock():
        loyalty.prepare_for_relay(paid[3])
    invitation.refresh_from_db()
    assert before == (invitation.pk, invitation.generation, invitation.dispatched_at)


def test_unused_expiry_renews_only_the_same_uuid_and_never_an_old_channel(paid):
    invitation = claim(paid)
    JoinInvitation.objects.filter(pk=invitation.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    renewed = claim(paid)
    assert renewed.pk == invitation.pk and renewed.generation == invitation.generation + 1
    JoinInvitation.objects.filter(pk=invitation.pk).update(
        channel="telegram", expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert claim(paid).channel == "telegram"


@pytest.mark.parametrize("state", ["unknown", "accepted", "delivered", "failed"])
def test_old_or_ambiguous_registry_attempt_never_becomes_a_fresh_permit(paid, state):
    invitation = claim(paid)
    JoinInvitation.objects.filter(pk=invitation.pk).update(
        state=state, dispatched_at=timezone.now(), expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert read(paid).get("domain_authorized") is not True
    with loyalty.source_lock():
        loyalty.prepare_for_relay(paid[3])
    invitation.refresh_from_db()
    assert invitation.generation == 1 and invitation.state == state


def test_consumption_and_expectation_rollback_together(prepared, monkeypatch):
    paid, invitation, proof, _, _ = prepared
    consume = registry.consume

    def changed(*a, **kw):
        assert consume(*a, **kw) == "eligible"
        raise ValidationError("synthetic-source-race")

    monkeypatch.setattr(registry, "consume", changed)
    with pytest.raises(ValidationError, match="synthetic-source-race"):
        receipts.reconcile(paid[3].pk, proof["request_id"], "synthetic")
    invitation.refresh_from_db()
    assert invitation.state == "claimed" and not invitation.dispatched_at
    assert not WebsiteMessageExpectation.objects.filter(request_id=proof["request_id"]).exists()


def test_conflicting_private_and_legacy_permits_and_provider_overflow_are_rejected(paid):
    invitation = claim(paid)
    with pytest.raises(ValidationError, match="website_invitation_permit_conflict"):
        registry.consume(
            invitation,
            phone=invitation.phone,
            website_message_expectation=object(),
            day_use_stay=paid[1],
        )
    with pytest.raises(ValidationError, match="provider_id_invalid"):
        registry.record(invitation, "accepted", "x" * 513)
    invitation.refresh_from_db()
    assert not invitation.provider_id and invitation.state == "claimed"


def test_public_claim_preflight_and_record_cannot_drive_private_website_channel(paid, monkeypatch):
    import hashlib

    from apps.loyalty.models import InvitationAPICredential
    from tests import test_loyalty_invitation_api as public

    invitation = claim(paid)
    InvitationAPICredential.objects.create(
        key_sha256=hashlib.sha256(public.TOKEN.encode()).hexdigest(),
        created_by=paid[1].created_by,
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    monkeypatch.setattr(invitation_api, "ready", lambda: True)
    assert public.post("claim", public.data(channel="ycloud")).status_code == 400
    assert (
        public.preflight(
            {"invitation_id": str(invitation.pk), "ticket": registry.ticket(invitation)},
            phone=invitation.phone,
        ).status_code
        == 404
    )
    assert (
        public.report(
            {
                "invitation_id": str(invitation.pk),
                "provider_reference": invitation.provider_reference,
            },
            "accepted",
        ).status_code
        == 404
    )
    invitation.refresh_from_db()
    assert invitation.state == "claimed" and not invitation.dispatched_at


def test_authenticated_read_exports_context_but_does_not_consume(paid, client):
    invitation = claim(paid)
    with CaptureQueriesContext(connection) as queries:
        response = original.request_source(client, paid[:3], "loyalty_invitation", financial=True)
    assert response.status_code == 200
    assert response.json()["template_context"]["loyalty_identity"]["invitation_id"] == str(
        invitation.pk
    )
    assert response.json()["domain_authorized"] is True
    assert response["Cache-Control"].startswith("private, no-store")
    assert response["Referrer-Policy"] == "no-referrer"
    assert not any(
        q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries
    )
    invitation.refresh_from_db()
    assert not invitation.dispatched_at


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_source_claims_keep_one_uuid(paid):
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL advisory and row locks")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    barrier = Barrier(2)
    row_id = paid[3].pk

    def run():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            with loyalty.source_lock():
                loyalty.prepare_for_relay(relay.WebsiteMessageRelay.objects.get(pk=row_id))
            row = JoinInvitation.objects.get()
            return str(row.pk), row.generation
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results[0] == results[1] and JoinInvitation.objects.count() == 1
