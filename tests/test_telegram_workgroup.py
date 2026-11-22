"""Synthetic workgroup copies; no provider, booking or payment mutations outside fixtures."""
# ruff: noqa: F811

import json
from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from apps.loyalty.models import JoinInvitation
from apps.reservations import telegram_authority as authority
from apps.reservations import telegram_backend as backend
from apps.reservations import telegram_stay_authority as bridge
from apps.reservations import telegram_stay_financial as financial
from apps.reservations import telegram_stay_journey as journey
from apps.reservations import telegram_stay_render as render
from apps.reservations import telegram_stay_source as source
from apps.reservations import telegram_workgroup as workgroup
from apps.reservations.telegram_stay_models import TelegramStayMessage, TelegramWorkgroupHandoff
from tests import test_message_source_authorization as fixtures
from tests import test_telegram_stay_journey as guest_tests
from tests import test_telegram_stay_source as source_tests
from tests.test_telegram_stay_transport import owned as owned  # noqa: F401

pytestmark = pytest.mark.django_db
record = fixtures.record
auth = source_tests.auth


@pytest.fixture
def group(settings):
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = True
    settings.TELEGRAM_ACTIVATION_ENABLED = False
    settings.TELEGRAM_LOYALTY_INVITATIONS_ENABLED = False
    settings.TELEGRAM_STAY_RECEIPTS_ENABLED = True


@pytest.fixture
def private_source(owned, group, monkeypatch):
    def read(row, action, **kwargs):
        assert isinstance(row, TelegramWorkgroupHandoff)
        assert TelegramWorkgroupHandoff.objects.filter(pk=row.pk).exists()
        if action == "handoff_status":
            return {"available": False}
        assert action == "render_source"  # No guest binding or phone proof lookup.
        text = next(
            t
            for t in guest_tests.TEMPLATES["templates"]
            if t["scenario"] == row.scenario and t["template_name"] == row.purpose
        )["content_" + row.identity["language"]]
        variables = dict.fromkeys(render.OPERATIONAL, "SYNTHETIC_ONLY")
        variables["timezone"] = "Asia/Riyadh"
        raw = json.dumps(variables, sort_keys=True)
        value = {
            **row.identity,
            "source_template_name": row.purpose,
            "source_template_text": text,
            "source_template_sha256": render.sha(text),
            "template_version": "sha256:" + render.sha(text),
            "variables": variables,
            "variables_canonical_json": raw,
            "variables_sha256": render.sha(raw),
        }
        canonical = json.dumps({k: value[k] for k in render.CANONICAL}, sort_keys=True)
        return {
            **value,
            "contract": "tg39_stay_render_source_v1",
            "available": True,
            "context_canonical_json": canonical,
            "context_sha256": render.sha(canonical),
            "authoritative_at": timezone.now().isoformat(),
            "expires_at": (timezone.now() + timedelta(seconds=25)).isoformat(),
            "dispatch_allowed": False,
        }

    monkeypatch.setattr(bridge, "read", read)
    return read


def next_packet(owned, event=None):
    data = source_tests.payload(
        owned, operation="next", **({"event_id": str(event.pk)} if event else {})
    )
    with transaction.atomic():
        packet = source.snapshot(data)
    return journey.next_packet(packet, data)


def proof(row, **changes):
    value = guest_tests.proof(row)
    value.pop("chat_binding_fingerprint")
    return {
        **value,
        "contract": "tg39_workgroup_handoff_v1",
        "channel": "workgroup_manual",
        "workgroup_id": workgroup.WORKGROUP_ID,
        "guest_accepted": False,
        "workgroup_binding_fingerprint": workgroup.fingerprint(),
        "status": "handed_off",
        "provider_message_ids": [str(value["provider_message_id"])],
        **changes,
    }


def handoff(owned, event=None):
    packet = next_packet(owned, event)
    assert packet["decision"] == "send"
    row = TelegramWorkgroupHandoff.objects.get(job_key=packet["job"]["job_key"])
    assert journey.import_evidence(row, proof(row))
    row.refresh_from_db()
    return row, packet


def test_group_copies_need_no_guest_binding_and_never_become_guest_acceptance(
    owned, private_source
):
    scenario, stay, _ = owned
    for seq in range(1, 4):
        row, packet = handoff(owned)
        row.refresh_from_db()
        assert row.state == "handed_off" and row.sequence == seq
        assert packet["dispatch_mode"] == "workgroup_manual"
        assert packet["workgroup_id"] == workgroup.WORKGROUP_ID
        assert packet["job"]["recipient_kind"] == "workgroup"
        assert packet["booking"]["guest_phone_e164"] == workgroup.canonical_phone(stay.guest_phone)
        assert "guest_phone_e164" not in row.booking_basis["booking"]
        assert journey.core_handed_off(stay, scenario) is (seq == 3)
        assert journey.accepted_core(stay, scenario) is False
    assert not TelegramStayMessage.objects.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("channel", "telegram"),
        ("workgroup_id", "-999"),
        ("guest_accepted", True),
        ("delivered", True),
        ("read", True),
        ("status", "accepted"),
        ("content_sha256", "a" * 64),
        ("phone_hash", "b" * 64),
        ("workgroup_binding_fingerprint", "c" * 64),
        ("source_revision", 999),
    ],
)
def test_forged_or_guest_receipts_cannot_advance_group(owned, private_source, field, value):
    packet = next_packet(owned)
    row = TelegramWorkgroupHandoff.objects.get(job_key=packet["job"]["job_key"])
    with pytest.raises(authority.Denied):
        journey.import_evidence(row, proof(row, **{field: value}))
    row.refresh_from_db()
    assert row.state == "planned" and row.attempt_id is None


def test_group_record_capability_cannot_adopt_guest_journal(owned, private_source):
    row, _ = handoff(owned)
    assert bridge.record_row(bridge.record_capability(row)).pk == row.pk
    assert bridge.scope_for(row)["kind"] == "stay_workgroup"
    with pytest.raises(authority.Denied):
        bridge.authorize(bridge.scope_for(row), "delivery_status", row.identity["stay_key"], {})
    result = bridge.authorize(bridge.scope_for(row), "handoff_status", row.identity["stay_key"], {})
    assert result["source_scope"] == "stay_workgroup"


def test_duplicate_and_unknown_group_attempt_do_not_create_new_identity(owned, private_source):
    packet = next_packet(owned)
    row = TelegramWorkgroupHandoff.objects.get(job_key=packet["job"]["job_key"])
    assert not journey.import_evidence(row, proof(row, status="unknown", api_accepted=False))
    assert next_packet(owned)["decision"] == "hold"
    assert TelegramWorkgroupHandoff.objects.count() == 1
    assert not TelegramStayMessage.objects.exists()


def test_missing_guest_phone_still_produces_group_copy_without_inventing_number(
    owned, private_source
):
    _, stay, _ = owned
    type(stay).objects.filter(pk=stay.pk).update(guest_phone="")
    stay.guest_phone = ""
    # Source event remains bound to its historical phone, so test the pure
    # snapshot projection separately; a changed event is not re-authorized.
    from unittest.mock import patch

    with patch.object(
        source, "resolve", return_value=(owned[0], stay, owned[2], 12345, "synthetic", False)
    ):
        packet = source.snapshot(source_tests.payload(owned, operation="snapshot"))
    assert packet["booking"]["guest_phone_e164"] is None
    assert packet["booking"]["phone_hash"] == render.sha("")


def test_real_payment_then_success_handoff_then_one_loyalty_handoff(
    owned, private_source, monkeypatch
):
    for _ in range(3):
        handoff(owned)
    paid = fixtures.paid_record(owned)
    row, packet = handoff(owned, paid[2])
    assert row.purpose == "payment_success" and not JoinInvitation.objects.exists()
    monkeypatch.setattr("apps.loyalty.invitation_api.ready", lambda: True)
    row, packet = handoff(owned, paid[2])
    assert row.purpose == "loyalty_invitation"
    invitation = JoinInvitation.objects.get()
    assert invitation.channel == "workgroup_manual" and invitation.state == "handed_off"
    assert not TelegramStayMessage.objects.exists()
    assert next_packet(owned, paid[2])["decision"] == "done"
    type(paid[1]).objects.filter(pk=paid[1].pk).update(amount_sar="999.00")
    with pytest.raises(authority.Denied):
        next_packet(owned, paid[2])


def test_manual_mode_closes_guest_activation_even_if_old_flags_are_true(group, settings):
    settings.TELEGRAM_ACTIVATION_ENABLED = True
    assert not backend.configured()


def test_permanent_policy_does_not_reopen_legacy_guest_senders_when_paused(settings):
    from types import SimpleNamespace

    from apps.reservations import guest_message_transport

    settings.TELEGRAM_WORKGROUP_ONLY_POLICY = True
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
    settings.TELEGRAM_ACTIVATION_ENABLED = True
    assert not backend.configured()
    assert workgroup.message_model() is TelegramWorkgroupHandoff
    with pytest.raises(guest_message_transport.TransportHeld, match="workgroup_manual_only"):
        guest_message_transport.prepare(
            SimpleNamespace(), source="morning07", purpose="status_new", reference="synthetic"
        )


def test_workgroup_long_copy_can_be_chunked_without_weakening_private_guest_limits():
    asset = {"asset_ref": "synthetic", "asset_sha256": "a" * 64, "asset_mime": "image/png"}
    for text, media in [("a" * 1025, asset), ("a" * 4097, None), ("😀" * 1500, asset)]:
        with pytest.raises(authority.Denied):
            render.message("en", text, media)
        result = render.message("en", text, media, manual_handoff=True)
        assert result["caption_html" if media else "body_html"] == text
    with pytest.raises(authority.Denied):
        render.message("en", "a" * 16385, manual_handoff=True)


def test_payment_resource_requires_three_group_handoffs_and_current_amount(
    owned, private_source, settings, monkeypatch
):
    scenario, stay, _ = owned
    links, domain, _ = financial.modules(scenario)
    settings.HYPERPAY_ENABLED = True
    settings.HYPERPAY_ENVIRONMENT = "production"
    monkeypatch.setattr(domain, "verify_window", lambda *a, **k: {})
    monkeypatch.setattr(domain, "local_inflight_conflict", lambda *a, **k: False)
    assert not financial.payable_link(stay, scenario)
    for _ in range(3):
        handoff(owned)
    packet = next_packet(owned)
    assert packet["job"]["purpose"] == "payment_link"
    assert financial.payable_link(stay, scenario)
    row = TelegramWorkgroupHandoff.objects.get(job_key=packet["job"]["job_key"])
    assert row.state == "planned" and row.attempt_id is None
    assert row.booking_basis["payment_fingerprint"] == links.fingerprint(stay)
    stay.amount_sar += 1
    assert not financial.payable_link(stay, scenario)


def test_record_endpoint_imports_only_the_private_group_proof(
    owned, private_source, auth, monkeypatch
):
    from django.test import Client

    result = next_packet(owned)
    row = TelegramWorkgroupHandoff.objects.get(job_key=result["job"]["job_key"])
    evidence = proof(row)
    monkeypatch.setattr(bridge, "read", lambda row, action, **kw: evidence)
    body = {
        "contract": "tg39_workgroup_record_request_v1",
        "scenario": row.scenario,
        "stay_key": row.identity["stay_key"],
        "source_revision": row.revision,
        "job_key": row.job_key,
        "reference_id": row.reference_id,
        "attempt_id": evidence["attempt_id"],
        "source_capability": result["job"]["source_capability"],
    }
    path = "/integrations/telegram39/stays/record/"
    client = Client(enforce_csrf_checks=True)
    for _ in range(2):
        reply = client.post(
            path, body, content_type="application/json", secure=True, **source_tests.loyalty.HEADERS
        )
        assert reply.status_code == 200
        assert reply.json()["contract"] == "tg39_workgroup_record_result_v1"
        assert reply.json()["recorded"] is True
        assert "no-store" in reply["Cache-Control"] and reply["Referrer-Policy"] == "no-referrer"
    assert client.post(path, body, content_type="application/json", secure=True).status_code == 401
    assert not TelegramStayMessage.objects.exists()


def test_historical_handoff_record_survives_end_and_phone_change_without_guest_send(
    owned, private_source, monkeypatch
):
    row, _ = handoff(owned)
    old = proof(
        row,
        attempt_id=str(row.attempt_id),
        provider_message_id=row.provider_message_id,
        accepted_at=row.accepted_at.isoformat(),
        recorded_at=None,
    )
    monkeypatch.setattr(bridge, "read", lambda row, action, **kw: old)
    type(owned[1]).objects.filter(pk=owned[1].pk).update(
        starts_at=timezone.now() - timedelta(hours=2),
        ends_at=timezone.now() - timedelta(hours=1),
        guest_phone="",
    )
    packet = journey.outstanding(row.scenario, str(row.stay_id))
    assert packet["decision"] == "record" and packet["job"]["recipient_kind"] == "workgroup"
    assert "method" not in packet["job"] and "guest_phone_e164" not in packet["booking"]
    assert packet["job"]["attempt_id"] == str(row.attempt_id)
    assert not TelegramStayMessage.objects.exists()


def test_old_guest_accepted_rows_never_release_workgroup_payment(owned, private_source):
    scenario, stay, _ = owned
    for _ in range(3):
        row, _ = handoff(owned)
        values = {
            field.name: getattr(row, field.name)
            for field in row._meta.fields
            if field.name not in {"id", "recipient_kind", "state"}
        }
        TelegramStayMessage.objects.create(**values, recipient_kind="guest", state="accepted")
    TelegramWorkgroupHandoff.objects.all().delete()
    assert not journey.core_handed_off(stay, scenario)
    assert not journey.accepted_core(stay, scenario)
    assert not financial.payable_link(stay, scenario)
