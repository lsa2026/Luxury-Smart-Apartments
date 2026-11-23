"""Private issued PDFs and synthetic provider proofs; no external send or booking."""

# ruff: noqa: F811

import base64
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

import httpx
import pytest
from django.core.exceptions import ValidationError

from apps.notifications.models import GuestInvoiceDelivery, SamaInvoiceDispatch
from apps.notifications.services import sama_invoice_source as source
from apps.notifications.services.invoice_delivery import deliver_invoice
from apps.reservations import sama_invoice_api as api
from tests.test_guest_invoices import payload, property_obj  # noqa: F401
from tests.test_sama_invoices import ENABLED, PHONE, post, request_data

pytestmark = pytest.mark.django_db


@pytest.fixture
def dispatch(payload, monkeypatch):
    with ENABLED, patch.object(api, "HostawayClient") as client:
        client.return_value.__enter__.return_value.get_reservation_invoice_data.return_value = (
            payload
        )
        result = post("send", request_data()).json()
        assert result["code"] == "invoice_requested", result
        row = SamaInvoiceDispatch.objects.get()
        monkeypatch.setattr(source.timezone, "now", lambda: row.not_before + timedelta(seconds=1))
        yield row


def scope(row):
    return {
        "phone": PHONE,
        "request_id": str(row.request_id),
        "delivery_id": str(row.delivery_id),
    }


def receipt(row, **changes):
    return {
        "contract": "sma19_invoice_receipt_v1",
        "transport": "ycloud",
        "request_id": str(row.request_id),
        "delivery_id": str(row.delivery_id),
        "invoice_id": str(row.delivery.invoice_id),
        "source_fingerprint": row.source_fingerprint,
        "recipient_sha256": row.recipient_sha256,
        "pdf_sha256": row.pdf_sha256,
        "caption_sha256": row.caption_sha256,
        "language": row.language,
        "outbox_id": "ad0f5dc3-84d1-41f8-8d7d-0a7b735b8a9c",
        "attempts_claimed": 1,
        "claimed_at": source.iso(row.not_before),
        "accepted_at": source.iso(row.not_before + timedelta(milliseconds=50)),
        "observed_at": source.iso(source.timezone.now()),
        "outcome": "accepted",
        "state": "accepted",
        "provider_message_id": "synthetic-provider",
        "wamid": "synthetic-wamid",
        "business_sender": "+966501234567",
        "waba_id": "123456789",
        "phone_id": "987654321",
        "receipt_evidence_ref": "synthetic-private-provider-proof",
        **changes,
    }


def record(row, proof, monkeypatch, **changes):
    fetched = Mock(return_value=proof)
    monkeypatch.setattr(source, "fetch_receipt", fetched)
    result = post("record", {**scope(row), "receipt_capability": "a" * 64, **changes})
    return result, fetched


def test_source_is_actual_frozen_pdf_private_and_same_guest_only(dispatch):
    row = dispatch
    value = post("document", scope(row))
    assert value.status_code == 200
    data = value.json()
    content = base64.b64decode(data["pdf_base64"])
    assert content.startswith(b"%PDF-") and source.sha(content) == row.pdf_sha256
    assert data["pdf_bytes"] == len(content)
    assert data["source_fingerprint"] == source.fingerprint(row)
    assert data["recipient_e164"] == PHONE
    assert value["Cache-Control"] == "no-store, private, max-age=0"
    assert value["Referrer-Policy"] == "no-referrer"
    for changes in ({"phone": "+966501234567"}, {"delivery_id": str(uuid.uuid4())}):
        denied = post("document", {**scope(row), **changes})
        assert denied.status_code == 409 and "pdf_base64" not in denied.json()
    denied = post("document", scope(row), HTTP_AUTHORIZATION="Bearer " + "cd" * 32)
    assert denied.status_code == 401 and denied["Referrer-Policy"] == "no-referrer"


def test_sender19_binding_never_constructs_ultramsg_or_claims_legacy_worker(dispatch):
    with patch("apps.notifications.services.invoice_delivery.UltraMsgClient") as legacy:
        assert deliver_invoice(dispatch.delivery_id) == "queued"
    legacy.assert_not_called()
    dispatch.delivery.refresh_from_db()
    assert dispatch.delivery.attempt_count == 0


def test_lost_send_ack_has_readonly_request_lookup_and_same_binding(dispatch):
    row = dispatch
    result = post("status", {"phone": PHONE, "request_id": str(row.request_id)}).json()
    assert result["dispatches"] == [source.projection(row)]
    assert result["deliveries"][0]["id"] == str(row.delivery_id)
    again, binding = source.queue(
        invoice=row.delivery.invoice,
        recipient=PHONE,
        actor=row.delivery.requested_by,
        request_id=row.request_id,
    )
    assert again.pk == row.delivery_id and binding.pk == row.pk
    assert GuestInvoiceDelivery.objects.count() == SamaInvoiceDispatch.objects.count() == 1


def test_existing_legacy_delivery_is_not_adopted_or_requeued(dispatch):
    delivery = dispatch.delivery
    dispatch.delete()
    delivery.status = "accepted"
    delivery.attempt_count = 1
    delivery.save()
    same, binding = source.queue(
        invoice=delivery.invoice,
        recipient=PHONE,
        actor=delivery.requested_by,
        request_id=uuid.uuid4(),
    )
    assert same.pk == delivery.pk and binding is None and same.status == "accepted"
    assert not SamaInvoiceDispatch.objects.exists()


def test_expired_or_modified_pdf_cannot_be_downloaded(dispatch, monkeypatch):
    row = dispatch
    monkeypatch.setattr(source.timezone, "now", lambda: row.expires_at + timedelta(seconds=1))
    assert post("document", scope(row)).status_code == 409
    monkeypatch.setattr(source.timezone, "now", lambda: row.not_before + timedelta(seconds=1))
    frozen = row.delivery.presentation if row.delivery.presentation_id else row.delivery.invoice
    frozen.pdf_content = b"%PDF-changed"
    frozen.save(update_fields=["pdf_content"])
    assert post("document", scope(row)).status_code == 409


def test_receipts_are_fetched_not_taken_from_caller_and_monotonic(dispatch, monkeypatch):
    row = dispatch
    accepted = receipt(row)
    result, fetched = record(row, accepted, monkeypatch)
    assert result.status_code == 200 and result.json()["receipt_imported"] is True
    assert result.json()["deliveries"][0]["status"] == "accepted"
    fetched.assert_called_once_with(str(row.request_id), "a" * 64)
    for state in ("delivered", "read", "sent", "accepted"):
        current = {**accepted, "state": state}
        value, _ = record(row, current, monkeypatch)
        assert value.status_code == 200
    row.refresh_from_db()
    assert row.provider_state == "read" and row.delivery.attempt_count == 1
    wrong = {**accepted, "outbox_id": str(uuid.uuid4())}
    assert record(row, wrong, monkeypatch)[0].status_code == 409
    value, fetched = record(row, accepted, monkeypatch, accepted=True)
    assert value.status_code == 400 and not fetched.called


@pytest.mark.parametrize(
    "changes",
    [
        {"pdf_sha256": "0" * 64},
        {"recipient_sha256": "0" * 64},
        {"invoice_id": str(uuid.uuid4())},
        {"source_fingerprint": "0" * 64},
        {"attempts_claimed": True},
        {"provider_message_id": None},
        {"claimed_at": "2000-01-01T00:00:00Z"},
    ],
)
def test_wrong_receipt_never_marks_accepted(dispatch, monkeypatch, changes):
    result, _ = record(dispatch, receipt(dispatch, **changes), monkeypatch)
    assert result.status_code == 409
    dispatch.delivery.refresh_from_db()
    assert dispatch.delivery.status == "queued" and dispatch.delivery.attempt_count == 0


@pytest.mark.parametrize("state", ["cancelled", "expired", "unknown"])
def test_preclaim_closure_never_invents_provider_attempt(dispatch, monkeypatch, state):
    proof = receipt(
        dispatch,
        state=state,
        outcome="unknown" if state == "unknown" else "rejected",
        attempts_claimed=0,
        claimed_at=None,
        accepted_at=None,
        provider_message_id=None,
        wamid=None,
    )
    result, _ = record(dispatch, proof, monkeypatch)
    assert result.status_code == 200
    dispatch.delivery.refresh_from_db()
    assert dispatch.delivery.status == ("review" if state == "unknown" else "failed")
    assert dispatch.delivery.attempt_count == 0 and dispatch.delivery.accepted_at is None


def test_historical_receipt_import_after_window_does_not_allow_pdf_resend(dispatch, monkeypatch):
    proof = receipt(dispatch)
    monkeypatch.setattr(source.timezone, "now", lambda: dispatch.expires_at + timedelta(days=1))
    assert record(dispatch, proof, monkeypatch)[0].status_code == 200
    assert post("document", scope(dispatch)).status_code == 409


def test_receipt_fetch_fixed_origin_no_redirect_and_bounded_capability(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://example.invalid/"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(source.httpx, "Client", lambda **kwargs: client)
    with pytest.raises(ValidationError):
        source.fetch_receipt(str(uuid.uuid4()), "a" * 64)
    assert len(requests) == 1
    assert requests[0].url.host == "sma-ycloud-test.onrender.com"
    assert requests[0].method == "GET" and not requests[0].url.query
    assert requests[0].headers["X-LSA-Receipt-Capability"] == "a" * 64


@ENABLED
def test_disabled_source_and_missing_request_id_do_not_issue(payload):
    with patch.object(api, "HostawayClient") as provider:
        with patch.object(source.settings, "SAMA_INVOICE_YCLOUD_ENABLED", False):
            assert post("send", request_data()).status_code == 503
        value = request_data()
        del value["request_id"]
        assert post("send", value).status_code == 400
        provider.assert_not_called()
