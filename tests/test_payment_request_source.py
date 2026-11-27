"""Ordinary payment source: disposable DB, synthetic identities, no provider/network."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.payments import payment_request_source as source
from apps.payments.models import PaymentAttempt, PaymentRequestDelivery
from apps.payments.payment_link_tasks import deliver_payment_request_task
from apps.payments.payment_links import issue_request
from apps.reservations import message_receipts as receipts
from apps.reservations import message_source_relay as relay
from apps.reservations.message_receipt_models import WebsiteMessageExpectation
from apps.reservations.message_relay_models import WebsiteMessageRelay
from apps.reservations.telegram_authority import Denied
from apps.reservations.telegram_website_workgroup import source_copy
from apps.reservations.website_template_policy_v2 import policy_from_source
from apps.reservations.website_text_access_v1 import selection_identity
from tests.test_hostaway_booking_phase5 import make_intent
from tests.test_reservation_payment_links import isolated_settings, manual_stay  # noqa: F401
from tests.test_stay_extensions import setup_extension  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def fresh(settings):
    settings.HYPERPAY_ENVIRONMENT = "production"
    settings.MESSAGE_SOURCE_V2_ENABLED = True
    settings.MESSAGE_SOURCE_AUTH_ENABLED = True
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.MESSAGE_RECEIPTS_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    at = (timezone.now() - timedelta(minutes=1)).isoformat()
    settings.MESSAGE_SOURCE_CUTOVER_AT = at
    settings.MESSAGE_PAYMENT_REQUEST_SOURCE_V2_ENABLED = True
    settings.MESSAGE_PAYMENT_REQUEST_SOURCE_CUTOVER_AT = at
    settings.OPERATIONS_EMAIL = "ops@example.test"
    reservation = manual_stay()
    reservation.property.name_en = "Synthetic apartment"
    reservation.property.name_fr = "Appartement fictif"
    reservation.property.save(update_fields=["name_en", "name_fr"])
    reservation.is_test = False
    reservation.save(update_fields=["is_test"])
    request = issue_request(reservation.pk)
    return reservation, request


def delivery(request, kind="link", sequence=1):
    return PaymentRequestDelivery.objects.create(request=request, kind=kind, sequence=sequence)


def paid(reservation, request, *, status="succeeded"):
    reservation.payment_status = "paid"
    reservation.save(update_fields=["payment_status"])
    request.status, request.paid_at = "paid", timezone.now()
    request.save(update_fields=["status", "paid_at"])
    return PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        payment_request=request,
        provider="hyperpay",
        provider_payment_id="synthetic-" + uuid4().hex,
        amount=request.amount_sar,
        currency="SAR",
        status=status,
        verified_at=timezone.now(),
        idempotency_key=uuid4().hex,
    )


def journal(request, kind="link"):
    row = delivery(request, kind)
    result = source.register(row.pk)
    assert result["status"] == "ycloud", result
    return WebsiteMessageRelay.objects.get(payment_request_delivery=row)


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("kind", ["link", "receipt"])
def test_private_source_and_independent_ordered_template(fresh, language, kind):
    reservation, request = fresh
    reservation.booking_intent.language = language
    reservation.booking_intent.save(update_fields=["language"])
    request.language = language
    from apps.payments.payment_links import fingerprint

    request.fingerprint = fingerprint(reservation)
    request.save(update_fields=["language", "fingerprint"])
    if kind == "receipt":
        paid(reservation, request)
    row = journal(request, kind)
    with transaction.atomic():
        original, facts = source.facts_for_row(row)
        assert facts["source_family"] == "website"
        assert facts["template_context"]["source_family"] == source.FAMILY
        assert facts["reservation_revision"] == request.fingerprint
        assert len(row.revision) == 64
        assert facts["domain_authorized"] is True
        policy = policy_from_source(original, facts, deepcopy(facts["template_context"]))
        assert policy["message"]["attachment"] is None
        assert policy["message"]["template_ref"] == (
            "lsa_payment_request_v2" if kind == "link" else "lsa_payment_received_v2"
        )
        assert policy["message"]["variables"][:1] == [facts["variables"]["guest_name"]]
        assert len(policy["message"]["variables"]) == (8 if kind == "link" else 5)
        if os.environ.get("LSA_PAYMENT_SOURCE_FIXTURE_DIRECTORY"):
            destination = Path(os.environ["LSA_PAYMENT_SOURCE_FIXTURE_DIRECTORY"])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"{kind}-{language}.json").write_text(
                json.dumps(
                    {
                        "synthetic_only": True,
                        "production_reads": False,
                        "facts": facts,
                        "selected": policy,
                        "source_binding": facts["source_binding"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        changed = deepcopy(facts["template_context"])
        changed["values"]["amount"] = "1.00"
        assert policy_from_source(original, facts, changed) is None
    assert source.register(original.pk)["relay_ref"] == str(row.pk)
    assert WebsiteMessageRelay.objects.count() == 1


@pytest.mark.parametrize(
    "status", ["sending", "sent", "handed_off", "unknown", "uncertain", "accepted", "failed"]
)
def test_no_adoption_of_any_previous_attempt_or_result(fresh, status):
    _, request = fresh
    old = delivery(request)
    old.status = status
    old.save(update_fields=["status"])
    assert source.register(old.pk)["status"] == "held"
    new = delivery(request, sequence=2)
    assert source.register(new.pk)["status"] == "held"
    assert not WebsiteMessageRelay.objects.exists()


@pytest.mark.parametrize(
    "changed", ["cutover", "paid_before_cutover", "disabled", "future_cutover"]
)
def test_new_lane_never_replays_older_or_paused_resources(fresh, settings, changed):
    reservation, request = fresh
    kind = "link"
    if changed == "paid_before_cutover":
        paid(reservation, request)
        request.paid_at = timezone.now() - timedelta(days=1)
        request.save(update_fields=["paid_at"])
        kind = "receipt"
    row = delivery(request, kind)
    if changed == "cutover":
        PaymentRequestDelivery.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
    elif changed == "disabled":
        settings.MESSAGE_PAYMENT_REQUEST_SOURCE_V2_ENABLED = False
    elif changed == "future_cutover":
        settings.MESSAGE_PAYMENT_REQUEST_SOURCE_CUTOVER_AT = (
            timezone.now() + timedelta(minutes=5)
        ).isoformat()
    assert source.register(row.pk)["status"] == "held"
    assert not WebsiteMessageRelay.objects.exists()


@pytest.mark.parametrize(
    "defect",
    [
        "flag_only",
        "provider",
        "verified",
        "amount",
        "currency",
        "intent",
        "refunded",
        "partially_refunded",
        "review",
        "unknown",
        "second_success",
        "booking_review",
    ],
)
def test_receipt_needs_current_unambiguous_collection(fresh, defect):
    reservation, request = fresh
    attempt = paid(reservation, request)
    if defect == "flag_only":
        attempt.delete()
    elif defect == "second_success":
        paid(reservation, request)
    elif defect == "booking_review":
        reservation.payment_status = "review"
        reservation.save(update_fields=["payment_status"])
    else:
        fields = {
            "provider": "provider_payment_id",
            "verified": "verified_at",
            "amount": "amount",
            "currency": "currency",
            "intent": "booking_intent_id",
        }
        values = {
            "provider": None,
            "verified": None,
            "amount": Decimal("1"),
            "currency": "USD",
            "intent": make_intent(property_obj=reservation.property).pk
            if defect == "intent"
            else None,
        }
        if defect in fields:
            setattr(attempt, fields[defect], values[defect])
            if defect == "currency":
                attempt.provider = "other"  # HyperPay's DB constraint already forbids non-SAR.
        else:
            attempt.status = defect
        attempt.save()
    with patch.object(source, "alert"):
        assert source.register(delivery(request, "receipt").pk)["status"] == "held"
    assert not WebsiteMessageRelay.objects.exists()


def test_central_task_has_no_group_or_legacy_fallback_and_pause_keeps_ownership(fresh, settings):
    _, request = fresh
    original = delivery(request)
    with patch("apps.reservations.telegram_website_workgroup.deliver_payment") as group:
        assert deliver_payment_request_task.run(original.pk)["status"] == "ycloud"
        group.assert_not_called()
    settings.MESSAGE_PAYMENT_REQUEST_SOURCE_V2_ENABLED = False
    with patch("apps.reservations.telegram_website_workgroup.deliver_payment") as group:
        assert deliver_payment_request_task.run(original.pk)["status"] == "ycloud"
        group.assert_not_called()
    with pytest.raises(Denied):
        source_copy("payment_request", str(original.pk))


def test_private_read_only_job_locator_ttl_and_binding(fresh, client):
    _, request = fresh
    row = journal(request)
    token = source.read_capability(row)
    url = reverse("integrations:payment_request_source_authorization")
    response = client.post(
        url,
        json.dumps({"job_sha256": row.job_sha256}),
        content_type="application/json",
        HTTP_X_LSA_PAYMENT_REQUEST_TOKEN=token,
    )
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "no-referrer"
    for body in [{"job_sha256": "0" * 64}, {"job_sha256": row.job_sha256, "amount": "1"}]:
        assert client.post(
            url,
            json.dumps(body),
            content_type="application/json",
            HTTP_X_LSA_PAYMENT_REQUEST_TOKEN=token,
        ).status_code in {400, 404}
    with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 121):
        assert (
            client.post(
                url,
                json.dumps({"job_sha256": row.job_sha256}),
                content_type="application/json",
                HTTP_X_LSA_PAYMENT_REQUEST_TOKEN=token,
            ).status_code
            == 404
        )
    assert not WebsiteMessageExpectation.objects.exists()


def test_ambiguous_relay_is_one_attempt_and_one_attention_email(fresh):
    _, request = fresh
    row = journal(request)
    client = Mock()
    client.post.side_effect = TimeoutError("not logged")
    with patch.object(source, "alert") as alert:
        relay.forward_due(client=client)
        relay.forward_due(client=client)
    assert client.post.call_count == 1
    alert.assert_called_once_with(row.payment_request_delivery_id, "uncertain")
    row.refresh_from_db()
    assert row.state == "uncertain"
    args = client.post.call_args
    assert args.args[0] == relay.RELAY_URL
    assert args.kwargs["json"]["source_kind"] == "payment_request"
    assert len(args.kwargs["json"]) == 12
    assert "X-LSA-Source-Read-Capability" in args.kwargs["headers"]


def test_current_source_change_before_post_blocks_and_does_not_rebind(fresh):
    reservation, request = fresh
    row = journal(request)
    reservation.booking_intent.guest_phone = "+16502530001"
    reservation.booking_intent.save(update_fields=["guest_phone"])
    client = Mock()
    with patch.object(source, "alert"):
        relay.forward_due(client=client)
    client.post.assert_not_called()
    row.refresh_from_db()
    assert row.state == "stale"
    assert WebsiteMessageRelay.objects.count() == 1


def test_zero_claim_ack_and_historical_delivery_never_mutate_money(fresh, monkeypatch, settings):
    reservation, request = fresh
    row = journal(request)
    row.state = "forwarding"
    row.save(update_fields=["state"])
    with transaction.atomic():
        original, facts = source.facts_for_row(row)
        policy = policy_from_source(original, facts, facts["template_context"])
    request_id = "n8n20-" + "a" * 64
    digest = receipts.content_digest(policy["language"], policy["message"])
    proof = {
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
            "language",
        )
    }
    proof.update(
        contract_version="yc19-receipt-proof-1",
        source="backend19",
        request_id=request_id,
        binding_ref="synthetic-binding",
        source_family=source.FAMILY,
        content_digest=digest,
        business_sender="synthetic",
        waba_id="2075336595945655",
        phone_id="synthetic-phone",
        transport="ycloud",
        proof_stage="intake_held",
        attempts_claimed=0,
        provider_attempted=False,
        provider_message_id=None,
        wamid=None,
        state="prepared",
        delivered=False,
        receipt_evidence_ref="",
        observed_at_epoch=None,
        prepare_ref="b" * 64,
        prepared_at_epoch=int(timezone.now().timestamp()),
        attempt_count_basis="durable_claim_not_observed_post_count",
        dispatch_allowed=False,
    )
    selection_ref, pack_sha = selection_identity(policy["message"])
    content = dict(
        contract_version="yc19-prepared-content-2",
        request_id=request_id,
        prepare_ref=proof["prepare_ref"],
        prepared_at_epoch=proof["prepared_at_epoch"],
        language=policy["language"],
        message_input=policy["message"],
        content_digest=digest,
        selection_ref=selection_ref,
        source_pack_sha256=pack_sha,
        content_binding_basis="planned_pending_independent_source_template_verification",
        dispatch_allowed=False,
    )
    view = dict(
        contract_version="yc19-render-source-view-2",
        request_id=request_id,
        prepare_ref=proof["prepare_ref"],
        prepared_at_epoch=proof["prepared_at_epoch"],
        context=facts["template_context"],
        source_provenance="authenticated_source01_financial",
        domain_recheck_required=True,
        dispatch_allowed=False,
    )

    def fetch(_rid, _cap, *, content=False, render_source=False):
        return deepcopy(view if render_source else prepared if content else proof)

    prepared = content
    monkeypatch.setattr(receipts, "fetch", fetch)
    assert receipts.initial_binding_relay(request_id, "synthetic-binding", "synthetic") == row.pk
    assert receipts.reconcile(row.pk, request_id, "synthetic") == "prepared"
    expectation = WebsiteMessageExpectation.objects.get()
    assert expectation.source_binding["delivery_uuid"] == str(row.event_id)
    assert (
        receipts.expectation_snapshot(expectation)["source_binding_sha256"]
        == row.source_binding_sha256
    )
    settings.MESSAGE_PAYMENT_REQUEST_SOURCE_V2_ENABLED = False
    request.status = "revoked"
    request.save(update_fields=["status"])
    proof.update(
        proof_stage="attempt_claimed",
        attempts_claimed=1,
        provider_attempted=True,
        provider_message_id="synthetic-provider",
        wamid="synthetic-wamid",
        state="accepted",
        delivered=False,
        receipt_evidence_ref="synthetic-signed-event",
        observed_at_epoch=int(timezone.now().timestamp()),
    )
    assert receipts.reconcile(row.pk, request_id, "synthetic") == "accepted"
    proof.update(state="failed", delivered=False)
    with patch.object(source, "alert"):
        assert receipts.reconcile(row.pk, request_id, "synthetic") == "held"
    original.refresh_from_db()
    assert original.status == "failed"
    proof.update(state="delivered", delivered=True)
    assert receipts.reconcile(row.pk, request_id, "synthetic") == "delivered"
    original.refresh_from_db()
    request.refresh_from_db()
    assert original.status == "delivered"
    assert request.status == "revoked" and request.paid_at is None
    assert not PaymentAttempt.objects.exists()


@pytest.mark.parametrize("offset,expected", [(-1, False), (0, True), (1, True)])
def test_dated_ownership_before_exact_after_and_pause(fresh, settings, offset, expected):
    _, request = fresh
    item = delivery(request)
    at = source.cutover()
    PaymentRequestDelivery.objects.filter(pk=item.pk).update(
        created_at=at + timedelta(seconds=offset)
    )
    assert source.routed(item.pk) is expected
    settings.MESSAGE_PAYMENT_REQUEST_SOURCE_V2_ENABLED = False
    assert source.routed(item.pk) is expected
    settings.MESSAGE_PAYMENT_REQUEST_SOURCE_CUTOVER_AT = (
        timezone.now() + timedelta(minutes=5)
    ).isoformat()
    assert source.routed(item.pk) is False


def test_one_redacted_email_for_source_hold_even_on_repeated_reads(fresh):
    from apps.notifications.models import EmailDelivery

    _, request = fresh
    item = delivery(request)
    source.alert(item.pk, "current_payment_source_unverified")
    source.alert(item.pk, "uncertain")
    notice = EmailDelivery.objects.get(
        idempotency_key="payment-request-source-attention:" + str(item.pk)
    )
    assert (
        notice.recipient_reference
        == f"payment-request-source:{item.pk}:current_payment_source_unverified"
    )
    assert "1650253" not in notice.recipient_reference


def test_explicit_repeat_cannot_create_alternate_central_sequence(fresh):
    from apps.payments.payment_link_tasks import queue_delivery

    _, request = fresh
    row = journal(request)
    for _ in range(2):
        assert queue_delivery(request.pk, resend=True).pk == row.payment_request_delivery_id
    assert request.deliveries.count() == 1


def test_nonempty_conflicting_attempt_modification_is_rejected(fresh, settings):
    from tests.test_payment_link_revisions import change

    settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED = True
    settings.HOSTAWAY_LIVE_EXTENSION_ENABLED = True
    reservation, request = fresh
    modification = change(reservation)
    attempt = paid(reservation, request)
    attempt.modification_request = modification
    attempt.save(update_fields=["modification_request"])
    with transaction.atomic(), pytest.raises(ValueError, match="payment_proof_mismatch"):
        source._receipt_attempt(request, reservation)


def test_predeploy_audit_only_reads_redacted_state(fresh, capsys):
    from importlib import import_module
    from types import SimpleNamespace

    from django.apps import apps

    reservation, request = fresh
    reservation.hostaway_reservation_id = 72385484  # Synthetic local fixture only.
    reservation.save(update_fields=["hostaway_reservation_id"])
    item = delivery(request)
    module = import_module(
        "apps.reservations.migrations.0045_websitemessagerelay_payment_request_delivery_and_more"
    )
    with patch.object(PaymentRequestDelivery, "save", side_effect=AssertionError("read only")):
        module.audit(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
    output = capsys.readouterr().out
    value = json.loads(output.split("LSA_PAYMENT_SOURCE_PREDEPLOY_AUDIT ", 1)[1])
    assert value["target72385484"][0]["request_status"] == "open"
    assert value["source_inflight"] == 0
    assert "1650253" not in output and "booking.example" not in output
    assert "guest_name" not in output and "provider_payment_id" not in output
    item.refresh_from_db()
    assert item.status == "queued"


def test_modified_delta_from_actual_checkout_flow_with_empty_attempt_modification(fresh, settings):
    from apps.payments.hyperpay.service import HyperPayService
    from apps.payments.payment_link_revisions import prepare_preview
    from tests.test_hyperpay import HyperPayStub
    from tests.test_payment_link_revisions import activate, change, collect
    from tests.test_reservation_payment_links import approved_document, checkout

    settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED = True
    settings.HOSTAWAY_LIVE_EXTENSION_ENABLED = True
    reservation, old = fresh
    collect(reservation, "500")
    modification = change(reservation)
    request, _ = activate(prepare_preview(reservation.pk, modification.pk))
    stub = HyperPayStub()
    session = checkout(request, stub=stub)
    assert session.attempt.modification_request_id is None
    stub.payment_document = approved_document(session.attempt)
    HyperPayService(client=stub).verify(session.attempt)
    request.refresh_from_db()
    assert request.status == "paid"
    row = journal(request, "receipt")
    with transaction.atomic():
        _, facts = source.facts_for_row(row)
    assert facts["variables"]["amount"] == "1642.00"
    assert facts["source_binding"]["modification_id"] == str(modification.pk)
    assert facts["source_binding"]["payment_attempt_id"] == str(session.attempt.pk)


@pytest.mark.django_db(transaction=True)
def test_concurrent_registration_uses_one_original_delivery(fresh):
    if connection.vendor != "postgresql":
        pytest.skip("Real row locks require PostgreSQL CI")
    _, request = fresh
    item = delivery(request)

    def work():
        close_old_connections()
        try:
            return source.register(item.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: work(), range(2)))
    assert results[0]["relay_ref"] == results[1]["relay_ref"]
    assert WebsiteMessageRelay.objects.count() == 1


def test_extension_child_uses_verified_ownership_without_modifying_parent(
    setup_extension,  # noqa: F811 -- imported pytest fixture
    settings,
    monkeypatch,
):
    from tests.test_stay_extensions import create_child

    settings.MESSAGE_SOURCE_V2_ENABLED = True
    settings.MESSAGE_SOURCE_AUTH_ENABLED = True
    settings.MESSAGE_SOURCE_RELAY_ENABLED = True
    settings.WEBSITE_GUEST_MESSAGE_TRANSPORT = "ycloud_central"
    settings.MESSAGE_SOURCE_CUTOVER_AT = (timezone.now() - timedelta(minutes=1)).isoformat()
    settings.MESSAGE_PAYMENT_REQUEST_SOURCE_CUTOVER_AT = settings.MESSAGE_SOURCE_CUTOVER_AT
    settings.MESSAGE_PAYMENT_REQUEST_SOURCE_V2_ENABLED = True
    case = setup_extension
    create_child(case, monkeypatch)
    reservation = case.extension.reservation
    original = deepcopy(case.extension.chain.root_snapshot)
    request = issue_request(reservation.pk)
    row = journal(request)
    with transaction.atomic():
        _, facts = source.facts_for_row(row)
    assert facts["source_binding"]["extension"]["ownership_acknowledged"] is True
    assert facts["reservation_key"] == str(reservation.hostaway_reservation_id)
    assert case.extension.chain.root_snapshot == original
    case.extension.ownership_acknowledged = False
    case.extension.save(update_fields=["ownership_acknowledged"])
    with transaction.atomic(), pytest.raises(ValueError):
        source.facts_for_row(row)
