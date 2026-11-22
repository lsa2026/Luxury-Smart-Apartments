"""Synthetic same-day composition, no provider or real guest operations."""
# ruff: noqa: F811

import json
import uuid
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import transaction
from django.utils import timezone

from apps.reservations import telegram_authority as authority
from apps.reservations import telegram_stay_authority as bridge
from apps.reservations import telegram_stay_journey as journey
from apps.reservations import telegram_stay_render as render
from apps.reservations import telegram_stay_source as source
from apps.reservations.telegram_stay_models import TelegramStayMessage
from tests import test_message_source_authorization as fixtures
from tests import test_telegram_stay_source as source_tests
from tests.test_telegram_stay_transport import owned as owned  # noqa: F401

pytestmark = pytest.mark.django_db
record = fixtures.record
auth = source_tests.auth
TEMPLATES = json.loads(
    (Path(__file__).parent / "fixtures/telegram_stay_templates.json").read_text()
)


def packet(owned):
    with transaction.atomic():
        return source.snapshot(source_tests.payload(owned, operation="next"))


def proof(row, **changes):
    return {
        "contract": "tg39_stay_delivery_status_v1",
        "available": True,
        "channel": "telegram",
        "status": "accepted",
        "api_accepted": True,
        **{
            k: row.identity[k]
            for k in (
                "job_key",
                "reference_id",
                "source_revision",
                "event_id",
                "purpose",
                "sequence",
                "stay_key",
                "source_owner",
                "source_key",
                "hostaway_reservation_id",
                "phone_hash",
            )
        },
        "content_sha256": row.content_sha256,
        "chat_binding_fingerprint": row.binding_fingerprint,
        "attempt_id": str(uuid.uuid4()),
        "provider_message_id": "12345",
        "accepted_at": timezone.now().isoformat(),
        "delivered": False,
        "read": False,
        **changes,
    }


@pytest.fixture
def private_source(owned, settings, monkeypatch):
    settings.TELEGRAM_ACTIVATION_ENABLED = True
    settings.TELEGRAM_STAY_RECEIPTS_ENABLED = True

    def read(row, action, **kwargs):
        if action == "delivery_status":
            return {"available": False}
        if action == "binding":
            return {
                "available": True,
                "user_id": "12345",
                "chat_id": "12345",
                "phone_hash": row.identity["phone_hash"],
                "revoked": False,
            }
        assert action == "render_source"
        assert TelegramStayMessage.objects.filter(pk=row.pk).exists()  # before any private read
        text = next(
            t
            for t in TEMPLATES["templates"]
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


def next_job(owned):
    value = packet(owned)
    return journey.next_packet(value, source_tests.payload(owned, operation="next"))


def accepted(owned):
    result = next_job(owned)
    assert result["decision"] == "send"
    row = TelegramStayMessage.objects.get(job_key=result["job"]["job_key"])
    assert journey.import_evidence(row, proof(row))
    return row, result


def test_three_core_messages_advance_only_after_exact_native_acceptance(owned, private_source):
    scenario, stay, _ = owned
    for sequence, purpose in enumerate(journey.CORE, 1):
        current = next_job(owned)
        assert current["job"]["purpose"] == purpose and current["job"]["sequence"] == sequence
        assert next_job(owned)["job"]["job_key"] == current["job"]["job_key"]
        row, result = accepted(owned)
        assert result["job"]["parse_mode"] == "HTML"
        assert result["job"]["method"] == (
            "photo" if scenario == "07" and sequence == 1 else "text"
        )
        assert journey.accepted_core(stay, scenario) is (sequence == 3)
    assert not any(k in result for k in ("core_delivered", "guest_read"))
    assert not stay.payment_link_deliveries.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("phone_hash", "0" * 64),
        ("content_sha256", "1" * 64),
        ("channel", "whatsapp"),
        ("source_revision", "1"),
        ("sequence", "1"),
        ("event_id", str(uuid.uuid4())),
        ("chat_binding_fingerprint", "2" * 64),
        ("source_owner", "foreign"),
        ("hostaway_reservation_id", "123"),
        ("delivered", True),
        ("read", True),
        ("api_accepted", False),
        ("provider_message_id", ""),
    ],
)
def test_forged_or_cross_channel_evidence_never_advances(owned, private_source, field, value):
    current = next_job(owned)
    row = TelegramStayMessage.objects.get(job_key=current["job"]["job_key"])
    with pytest.raises((authority.Denied, ValueError)):
        journey.import_evidence(row, proof(row, **{field: value}))
    row.refresh_from_db()
    assert row.state == "planned" and row.attempt_id is None
    assert next_job(owned)["job"]["sequence"] == 1


def test_uncertain_attempt_holds_without_reposting_or_new_identity(owned, private_source):
    current = next_job(owned)
    row = TelegramStayMessage.objects.get(job_key=current["job"]["job_key"])
    assert not journey.import_evidence(
        row,
        proof(
            row, status="unknown", api_accepted=False, provider_message_id=None, accepted_at=None
        ),
    )
    result = next_job(owned)
    assert result["decision"] == "hold" and "job" not in result
    assert TelegramStayMessage.objects.count() == 1


def test_historical_accepted_attempt_can_be_recorded_after_end_and_phone_change(
    owned, private_source, monkeypatch, auth, client
):
    current = next_job(owned)
    row = TelegramStayMessage.objects.get(job_key=current["job"]["job_key"])
    evidence = proof(row)
    scenario, stay, _ = owned
    model = type(stay)
    model.objects.filter(pk=stay.pk).update(
        starts_at=timezone.now() - timedelta(days=2),
        ends_at=timezone.now() - timedelta(days=1),
        guest_phone="+12025550999",
    )
    monkeypatch.setattr(bridge, "read", lambda *a, **k: evidence)
    pending = journey.outstanding(scenario, stay.pk)
    assert (
        pending["decision"] == "record"
        and pending["booking"]["phone_hash"] == row.identity["phone_hash"]
    )
    body = {
        "contract": "tg39_stay_record_request_v1",
        "scenario": scenario,
        "stay_key": row.identity["stay_key"],
        "source_revision": row.revision,
        "job_key": row.job_key,
        "reference_id": row.reference_id,
        "attempt_id": evidence["attempt_id"],
        "source_capability": pending["job"]["source_capability"],
    }
    from tests.test_loyalty_invitation_api import HEADERS

    for _ in range(2):
        response = client.post(
            "/integrations/telegram39/stays/record/",
            body,
            content_type="application/json",
            secure=True,
            **HEADERS,
        )
        assert response.status_code == 200 and response.json()["recorded"] is True
    row.refresh_from_db()
    assert row.state == "accepted" and not journey.accepted_core(
        model.objects.get(pk=stay.pk), scenario
    )


def test_render_and_receipt_authorities_are_action_job_and_channel_scoped(owned, private_source):
    current = next_job(owned)
    row = TelegramStayMessage.objects.get(job_key=current["job"]["job_key"])
    scope = bridge.scope_for(row)
    envelope = authority.mint(scope, "render_source", row.identity["stay_key"])
    envelope["contract"] = "tg39_activation_readback_v1"
    response = authority.authorize(envelope)
    assert response["source_scope"] == "stay_message" and response["job_key"] == row.job_key
    assert "phone" not in response and "evidence_ref" not in response
    for action in ("approve_phone", "issue_invitation"):
        with pytest.raises(authority.Denied):
            bridge.authorize(scope, action, row.identity["stay_key"], {})
    wrong = deepcopy(envelope)
    wrong["action"] = "delivery_status"
    with pytest.raises(authority.Denied):
        authority.authorize(wrong)


def test_payment_link_waits_for_three_and_uses_separate_acceptance_release(
    owned, private_source, settings, monkeypatch
):
    from apps.reservations.telegram_stay_financial import modules, payable_link

    scenario, stay, _ = owned
    links, domain, _ = modules(scenario)
    settings.HYPERPAY_ENABLED = True
    settings.HYPERPAY_ENVIRONMENT = "production"
    monkeypatch.setattr(domain, "verify_window", lambda *a, **k: {})
    monkeypatch.setattr(domain, "local_inflight_conflict", lambda *a, **k: False)
    for _ in range(3):
        accepted(owned)
    result = next_job(owned)
    assert result["job"]["purpose"] == "payment_link"
    assert not payable_link(stay, scenario)
    row = TelegramStayMessage.objects.get(job_key=result["job"]["job_key"])
    assert row.booking_basis["payment_fingerprint"] == links.fingerprint(stay)
    journey.import_evidence(row, proof(row))
    assert payable_link(stay, scenario) and not stay.payment_link_deliveries.exists()
    stay.amount_sar += 1
    assert not payable_link(stay, scenario)


def test_approved_financial_copy_escapes_guest_markup_before_static_bold(owned):
    from apps.reservations.telegram_stay_financial import modules, trusted_copy

    scenario, stay, _ = owned
    stay.guest_name = "</b><script>SYNTHETIC</script> *name*"
    text = trusted_copy(modules(scenario)[0].render_message, stay)
    assert "<script>" not in text and "&lt;script&gt;" in text and "*name*" in text


@pytest.mark.parametrize("prefix", ["h", "d"])
def test_same_day_prefixes_cannot_be_used_by_ordinary_activation(prefix):
    from apps.reservations.telegram_backend import Unavailable, checked_invitation

    value = {
        "url": "https://t.me/LuxurySmartApartmentsBot?start=" + prefix + "_" + "a" * 48,
        "expires_at": (timezone.now() + timedelta(hours=2)).isoformat(),
    }
    with pytest.raises(Unavailable):
        checked_invitation(value)
    assert checked_invitation(value, prefixes=prefix)[0] == value["url"]
