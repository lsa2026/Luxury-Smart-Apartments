from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from django.core import signing
from django.db import connection
from django.utils import timezone

from apps.reservations import extension_checkout_source_v2 as ext
from tests import test_stay_extensions as original

setup_extension = original.setup_extension


@pytest.mark.parametrize("zone", ["Asia/Riyadh", "Africa/Casablanca"])
@pytest.mark.parametrize("departure", ["2027-03-01", "2027-04-10", "2027-11-22"])
def test_due_is_original_local_previous_evening_not_worker_now(zone, departure):
    end = datetime.fromisoformat(departure + "T12:00:00").replace(tzinfo=ZoneInfo(zone))
    due, actual_end = ext.original_schedule(end.isoformat(), zone)
    assert due.tzinfo is UTC and actual_end == end
    local = due.astimezone(ZoneInfo(zone))
    assert local.hour == 21 and local.minute == 0
    assert local.date() == end.date() - timedelta(days=1)
    assert ext.job_key(uuid4(), "90000002", end.isoformat(), zone).endswith(
        ":90000002:" + departure
    )


@pytest.fixture
def packet():
    now = timezone.now()
    row = SimpleNamespace(
        pk=uuid4(),
        event_id=uuid4(),
        revision="7",
        job_sha256="a" * 64,
        not_before=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=2),
        state="forwarding",
    )
    extension = SimpleNamespace(
        pk=uuid4(), chain_id=uuid4(), chain=SimpleNamespace(fingerprint="b" * 64, revision=7)
    )
    row.stay_id = extension.chain_id
    return now, row, extension


def test_fresh_read_is_bounded_to_job_event_chain_and_120_seconds(packet):
    now, row, extension = packet
    capability = ext.read_capability(row, extension, now=now)
    claims = ext.read_claims(capability, row.job_sha256, now=now)
    assert set(claims) == ext.CLAIM_KEYS
    assert claims["event_id"] == str(row.event_id)
    assert claims["chain_id"] == str(extension.chain_id)
    assert claims["revision"] == row.revision
    assert claims["expires_at"] - claims["issued_at"] == 120
    with pytest.raises(ValueError):
        ext.read_claims(capability, "c" * 64, now=now)
    with pytest.raises(ValueError):
        ext.read_claims(capability, row.job_sha256, now=now + timedelta(seconds=121))
    with pytest.raises(ValueError):
        ext.read_claims(capability, row.job_sha256, now=now - timedelta(seconds=2))


def test_read_expiry_never_outlives_source_window(packet):
    now, row, extension = packet
    row.expires_at = now + timedelta(seconds=10)
    claims = ext.read_claims(ext.read_capability(row, extension, now=now), row.job_sha256, now=now)
    assert claims["expires_at"] == int(row.expires_at.timestamp())


@pytest.mark.parametrize("change", ["not_due", "expired", "accepted", "uncertain", "held"])
def test_minting_never_resumes_a_past_attempt_or_moves_due(packet, change):
    now, row, extension = packet
    original_due = row.not_before
    if change == "not_due":
        row.not_before = now + timedelta(seconds=1)
    elif change == "expired":
        row.expires_at = now
    else:
        row.state = change
    with pytest.raises(ValueError):
        ext.read_capability(row, extension, now=now)
    if change != "not_due":
        assert row.not_before == original_due


def test_old_ownership_or_event_capability_is_not_checkout_read_authority(packet):
    now, row, extension = packet
    old = signing.dumps(
        {
            "extension_id": str(extension.pk),
            "event_id": str(row.event_id),
            "expires": int((now + timedelta(days=90)).timestamp()),
        },
        salt=ext.SALT,
    )
    with pytest.raises(ValueError):
        ext.read_claims(old, row.job_sha256, now=now)


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 7),
        ("revision", "0"),
        ("revision", "07"),
        ("chain_fingerprint", "invalid"),
        ("relay_ref", str(uuid4()).upper()),
        ("expires_at", True),
        ("job_sha256", ""),
    ],
)
def test_signed_but_malformed_scope_is_not_authority(packet, field, value):
    now, row, extension = packet
    claims = ext.read_claims(ext.read_capability(row, extension, now=now), row.job_sha256, now=now)
    claims[field] = value
    with pytest.raises((ValueError, TypeError)):
        ext.read_claims(signing.dumps(claims, salt=ext.SALT), row.job_sha256, now=now)


@pytest.fixture
def active(db, setup_extension, monkeypatch):
    case = setup_extension
    original.create_child(case, monkeypatch)
    original.pay(case)
    case.extension.refresh_from_db()
    case.event = case.extension.events.get(revision=case.extension.chain.revision)
    return case


def test_basis_rechecks_real_chain_and_existing_live_reader_without_writes(active):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as queries:
        event, extension, context, phone, key, due, end = ext.current_basis(active.event.pk)
    assert event.pk == active.event.pk and extension.pk == active.extension.pk
    assert phone == "+966500000000"
    assert str(extension.reservation.hostaway_reservation_id) == str(
        context["terminal_hostaway_reservation_id"]
    )
    assert key.startswith(f"extension:checkout:{extension.chain_id}:")
    assert due.astimezone(ZoneInfo(context["timezone"])).hour == 21
    assert end == datetime.fromisoformat(context["effective_checkout_at"])
    assert not any(
        q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"} for q in queries
    )


@pytest.mark.parametrize("change", ["revision", "review", "unpaid", "cancelled", "test", "owner"])
def test_basis_cannot_override_original_booking_collection_or_current_chain(active, change):
    from apps.reservations.models import StayExtension, StayExtensionChain

    extension = active.extension
    if change == "revision":
        StayExtensionChain.objects.filter(pk=extension.chain_id).update(
            revision=extension.chain.revision + 1
        )
    elif change == "review":
        StayExtensionChain.objects.filter(pk=extension.chain_id).update(review_required=True)
    elif change == "unpaid":
        type(extension.reservation).objects.filter(pk=extension.reservation_id).update(
            payment_status="unpaid"
        )
    elif change == "cancelled":
        StayExtension.objects.filter(pk=extension.pk).update(state="cancelled")
    elif change == "test":
        type(extension.reservation).objects.filter(pk=extension.reservation_id).update(is_test=True)
    else:
        StayExtension.objects.filter(pk=extension.pk).update(ownership_acknowledged=False)
    with pytest.raises(ValueError):
        ext.current_basis(active.event.pk)


def test_live_provider_mismatch_never_uses_stored_context_as_authority(active, monkeypatch):
    monkeypatch.setattr(
        "apps.reservations.stay_extension_communications.read_source", lambda *a, **k: {}
    )
    with pytest.raises(ValueError, match="extension_terminal_not_verified"):
        ext.current_basis(active.event.pk)


def enable(settings):
    settings.MESSAGE_EXTENSION_SOURCE_V2_ENABLED = True
    settings.MESSAGE_SOURCE_V2_ENABLED = True
    settings.MESSAGE_SOURCE_AUTH_ENABLED = True
    settings.MESSAGE_RECEIPTS_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.MESSAGE_EXTENSION_SOURCE_CUTOVER_AT = (
        timezone.now() - timedelta(minutes=1)
    ).isoformat()


@pytest.fixture
def due(active, settings, monkeypatch):
    from apps.reservations.message_relay_models import WebsiteMessageRelay

    enable(settings)
    ext.register_event(active.event)
    row = WebsiteMessageRelay.objects.get(scenario="02")
    current = row.not_before + timedelta(seconds=1)
    monkeypatch.setattr(ext.timezone, "now", lambda: current)
    return active, row


@pytest.mark.django_db
def test_actual_capture_registers_once_with_current_chain_revision(
    setup_extension, settings, monkeypatch, db, django_capture_on_commit_callbacks
):
    from apps.reservations.message_relay_models import WebsiteMessageRelay

    original.create_child(setup_extension, monkeypatch)
    enable(settings)
    with django_capture_on_commit_callbacks(execute=True):
        original.pay(setup_extension)
    setup_extension.extension.refresh_from_db()
    row = WebsiteMessageRelay.objects.get(scenario="02")
    assert row.revision == str(setup_extension.extension.chain.revision)
    original.refresh_chain(setup_extension.extension.chain_id)
    assert WebsiteMessageRelay.objects.count() == 1


@pytest.mark.django_db
def test_source_journal_failure_cannot_roll_back_real_payment_state(
    setup_extension, settings, monkeypatch, django_capture_on_commit_callbacks
):
    original.create_child(setup_extension, monkeypatch)
    enable(settings)

    def unavailable(event):
        raise RuntimeError("synthetic_journal_unavailable")

    monkeypatch.setattr(ext, "register_event", unavailable)
    with django_capture_on_commit_callbacks(execute=True):
        original.pay(setup_extension)
    setup_extension.extension.refresh_from_db()
    assert setup_extension.extension.state == "active"
    assert setup_extension.extension.reservation.payment_status == "paid"


def test_existing_source_event_is_not_retimed_or_adopted_after_cutover(active, settings):
    from apps.reservations.message_relay_models import WebsiteMessageRelay

    enable(settings)
    settings.MESSAGE_EXTENSION_SOURCE_CUTOVER_AT = (
        timezone.now() + timedelta(seconds=1)
    ).isoformat()
    ext.register_event(active.event)
    assert not WebsiteMessageRelay.objects.exists()


def test_private_read_preserves_source_identity_and_unknown_external_history(due, client):
    from django.urls import reverse

    active, row = due
    row.state = "forwarding"
    row.save(update_fields=["state"])
    cap = ext.read_capability(row, active.extension)
    reply = client.post(
        reverse("integrations:extension_checkout_source_authorization"),
        {"job_sha256": row.job_sha256},
        content_type="application/json",
        headers={ext.HEADER: cap},
    )
    assert reply.status_code == 200, reply.content
    f = reply.json()
    assert f["source_event_key"] == "website:extension:checkout:" + str(active.event.pk)
    assert f["reservation_key"] == str(active.extension.reservation.hostaway_reservation_id)
    assert f["reservation_revision"] == row.revision
    assert f["legacy_state"] == "unknown" and f["legacy_history_owner"] == "source20"
    assert f["domain_authorized"] is True and f["dispatch_allowed"] is False
    assert parse_time(f["not_before"]) == row.not_before
    assert reply["Referrer-Policy"] == "no-referrer" and "no-store" in reply["Cache-Control"]
    assert (
        client.post(
            reverse("integrations:extension_checkout_source_authorization"),
            {"job_sha256": row.job_sha256},
            content_type="application/json",
        ).status_code
        == 404
    )


def parse_time(value):
    return datetime.fromisoformat(value)


@pytest.fixture
def planned(due, monkeypatch):
    from copy import deepcopy

    from apps.reservations import message_receipts as receipts
    from apps.reservations import website_template_selector_v2 as selector

    active, row = due
    row.state = "forwarding"
    row.save(update_fields=["state"])
    event, facts = ext.facts_for_row(row)
    context = {
        k: facts[k]
        for k in (
            "scenario",
            "source_family",
            "job_sha256",
            "reservation_key",
            "reservation_revision",
            "source_event_key",
            "source_message_type",
            "purpose",
            "language",
            "not_before",
            "expires_at",
        )
    }
    context.update(
        recipient_ref=None,
        recipient_binding_sha256=ext.source.sha(
            facts["reservation_key"] + ":" + facts["recipient_e164"]
        ),
        source_context_ref="yc20-template-source-2:synthetic-extension",
        preflight_status="ready",
        legacy_state="not_attempted",
        legacy_evidence_ref="yc20-ext-legacy-v1:" + "e" * 64,
        values=dict(facts["variables"]),
    )
    policy = ext.policy_from_source(event, facts, context)
    assert policy is not None
    rid = "n8n20-" + "a" * 64
    proof = {
        "contract_version": "yc19-receipt-proof-1",
        "source": "backend19",
        "language": facts["language"],
        "attempt_count_basis": "durable_claim_not_observed_post_count",
        "dispatch_allowed": False,
        "request_id": rid,
        "binding_ref": "synthetic-extension-binding",
        "source_family": ext.FAMILY,
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
        "content_digest": receipts.content_digest(policy["language"], policy["message"]),
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
        "prepared_at_epoch": int(timezone.now().timestamp()) - 1,
    }
    content = {
        "contract_version": "yc19-prepared-content-2",
        "request_id": rid,
        "prepare_ref": proof["prepare_ref"],
        "prepared_at_epoch": proof["prepared_at_epoch"],
        "language": policy["language"],
        "message_input": policy["message"],
        "content_digest": proof["content_digest"],
        "dispatch_allowed": False,
        "selection_ref": selector.SELECTION_REF,
        "source_pack_sha256": selector.PACK_SHA,
        "content_binding_basis": "planned_pending_independent_source_template_verification",
    }

    def fetch(request_id, capability, *, content=False, render_source=False):
        assert request_id == rid
        if render_source:
            return {
                "contract_version": "yc19-render-source-view-2",
                "request_id": rid,
                "prepare_ref": proof["prepare_ref"],
                "prepared_at_epoch": proof["prepared_at_epoch"],
                "context": deepcopy(context),
                "source_provenance": "private_runtime_rpc_source20",
                "domain_recheck_required": True,
                "dispatch_allowed": False,
            }
        return deepcopy(prepared_content if content else proof)

    prepared_content = content
    monkeypatch.setattr(receipts, "fetch", fetch)
    return active, row, facts, context, proof, content


def test_real_source_ack_reconstructs_template_before_claim_and_keeps_historical_import(
    planned, settings
):
    from apps.reservations import message_receipts as receipts
    from apps.reservations.message_receipt_models import WebsiteMessageExpectation
    from tests.test_message_receipts import deliver

    active, row, _, _, proof, _ = planned
    assert (
        receipts.initial_binding_relay(proof["request_id"], proof["binding_ref"], "synthetic")
        == row.pk
    )
    assert receipts.reconcile(row.pk, proof["request_id"], "synthetic") == "prepared"
    prior = WebsiteMessageExpectation.objects.get()
    assert prior.identity["purpose"] == "checkout_reminder" and prior.attempts_claimed == 0
    settings.MESSAGE_EXTENSION_SOURCE_V2_ENABLED = False
    settings.MESSAGE_SOURCE_V2_ENABLED = False
    type(active.extension).objects.filter(pk=active.extension.pk).update(state="cancelled")
    deliver(proof)
    assert receipts.reconcile(row.pk, proof["request_id"], "synthetic") == "delivered"
    assert receipts.reconcile(row.pk, proof["request_id"], "synthetic") == "delivered"
    assert WebsiteMessageExpectation.objects.count() == 1


@pytest.mark.parametrize(
    "change", ["history", "reference", "phone", "revision", "value", "claimed", "held"]
)
def test_source_ack_never_promotes_missing_history_or_changed_scope(planned, change):
    from apps.reservations import message_receipts as receipts
    from apps.reservations.message_receipt_models import WebsiteMessageExpectation

    _, row, _, context, proof, _ = planned
    if change == "history":
        context["legacy_state"] = "unknown"
    elif change == "reference":
        context.pop("legacy_evidence_ref")
    elif change == "phone":
        context["recipient_binding_sha256"] = "0" * 64
    elif change == "revision":
        context["reservation_revision"] = "999"
    elif change == "value":
        context["values"]["guest_name"] = "changed synthetic"
    elif change == "claimed":
        proof.update(attempts_claimed=1, proof_stage="attempt_claimed")
    else:
        row.state = "held"
        row.save(update_fields=["state"])
    try:
        result = receipts.reconcile(row.pk, proof["request_id"], "synthetic")
        assert result == "held"
    except ValueError:
        pass
    assert not WebsiteMessageExpectation.objects.exists()


def test_future_worker_passes_fresh_read_in_header_only_and_ack_is_not_delivery(due, settings):
    from types import SimpleNamespace

    from apps.reservations import message_source_relay as relay
    from apps.reservations.message_receipt_models import WebsiteMessageExpectation

    _, row = due
    settings.MESSAGE_SOURCE_RELAY_ENABLED = False
    calls = []
    # A shared worker must leave inactive07/08 journals alone while EXT is enabled.
    untouched = relay.WebsiteMessageRelay.objects.create(
        scenario="07",
        event_id=uuid4(),
        stay_id=uuid4(),
        revision="1",
        message_type="status_new",
        job_sha256="f" * 64,
        source_occurred_at=row.source_occurred_at,
        not_before=row.not_before,
        expires_at=row.expires_at,
    )

    def post(url, *, json, headers):
        assert url == relay.RELAY_URL
        assert set(json) == {
            "contract_version",
            "relay_ref",
            "scenario",
            "source_kind",
            "source_event_id",
            "source_event_key",
            "source_occurred_at",
            "job_sha256",
            "source_message_type",
            "reservation_revision",
            "not_before",
            "expires_at",
        }
        assert json["source_kind"] == "extension_checkout" and json["scenario"] == "02"
        claims = ext.read_claims(headers[relay.READ_HEADER], row.job_sha256)
        assert claims["relay_ref"] == str(row.pk)
        calls.append(json)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "contract_version": "site01-source-relay-ack-2",
                "relay_ref": str(row.pk),
                "job_sha256": row.job_sha256,
                "accepted": True,
                "status": "bound",
                "request_id": "n8n20-" + "a" * 64,
            },
        )

    assert relay.forward_due(client=SimpleNamespace(post=post)) == {"accepted": 1}
    row.refresh_from_db()
    untouched.refresh_from_db()
    assert row.state == "accepted" and untouched.state == "pending" and len(calls) == 1
    assert not WebsiteMessageExpectation.objects.exists()


def test_valid_read_capability_cannot_survive_new_phone_and_chain_revision(due, client):
    from django.urls import reverse

    active, row = due
    row.state = "forwarding"
    row.save(update_fields=["state"])
    cap = ext.read_capability(row, active.extension)
    type(active.extension.draft).objects.filter(pk=active.extension.draft_id).update(
        guest_phone="+966500000001"
    )
    original.refresh_chain(active.extension.chain_id)
    reply = client.post(
        reverse("integrations:extension_checkout_source_authorization"),
        {"job_sha256": row.job_sha256},
        content_type="application/json",
        headers={ext.HEADER: cap},
    )
    assert reply.status_code == 404


def test_old_ambiguous_attempt_cannot_be_hidden_by_fresh_event_uuid(due):
    from apps.reservations.message_receipt_models import WebsiteMessageExpectation
    from apps.reservations.message_relay_models import WebsiteMessageRelay

    _, row = due
    old = WebsiteMessageRelay.objects.create(
        scenario="02",
        event_id=uuid4(),
        stay_id=row.stay_id,
        revision="1",
        message_type="reminder_checkout",
        job_sha256=row.job_sha256,
        source_occurred_at=row.source_occurred_at,
        not_before=row.not_before,
        expires_at=row.expires_at,
        state="uncertain",
    )
    WebsiteMessageExpectation.objects.create(
        request_id="n8n20-" + "f" * 64,
        relay=old,
        prepare_ref="f" * 64,
        identity={},
        render_policy_ref="synthetic-prior-policy",
        prepared_at_epoch=int(timezone.now().timestamp()),
        attempts_claimed=1,
        state="uncertain",
    )
    _, facts = ext.facts_for_row(row)
    assert facts["domain_authorized"] is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires real PostgreSQL row locks")
def test_live_chain_read_holds_revision_lock_until_domain_verification_finishes(
    active, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections

    from apps.reservations import stay_extension_communications as comms
    from apps.reservations.models import StayExtensionChain

    original_context = comms.context
    started, finished = Event(), Event()
    pending = []

    def update_chain():
        close_old_connections()
        try:
            started.set()
            return StayExtensionChain.objects.filter(pk=active.extension.chain_id).update(
                revision=active.event.revision + 1
            )
        finally:
            finished.set()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def current(extension, **kwargs):
            pending.append(pool.submit(update_chain))
            assert started.wait(2)
            assert not finished.wait(0.1)
            return original_context(extension, **kwargs)

        monkeypatch.setattr(comms, "context", current)
        ext.current_basis(active.event.pk)
        assert pending[0].result(timeout=3) == 1


def test_new_source_event_after_original_due_keeps_due_and_does_not_replay_history(
    active, settings, monkeypatch
):
    from apps.reservations.message_relay_models import WebsiteMessageRelay

    _, _, context, _, _, due, end = ext.current_basis(active.event.pk)
    occurred = due + timedelta(minutes=2)
    monkeypatch.setattr(ext.timezone, "now", lambda: occurred)
    enable(settings)
    type(active.event).objects.filter(pk=active.event.pk).update(created_at=occurred)
    active.event.refresh_from_db()
    ext.register_event(active.event)
    row = WebsiteMessageRelay.objects.get(scenario="02")
    assert row.not_before == due < row.source_occurred_at == occurred < row.expires_at == end
    event, facts = ext.facts_for_row(row)
    assert event.pk == active.event.pk and facts["domain_authorized"] is True
    assert (
        facts["not_before"] == due.isoformat()
        and facts["source_occurred_at"] == occurred.isoformat()
    )
    assert facts["legacy_state"] == "unknown"
    settings.MESSAGE_EXTENSION_SOURCE_CUTOVER_AT = (occurred + timedelta(seconds=1)).isoformat()
    WebsiteMessageRelay.objects.all().delete()
    ext.register_event(active.event)
    assert not WebsiteMessageRelay.objects.exists()
