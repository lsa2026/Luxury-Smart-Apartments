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


def test_real_payment_proof_then_success_acceptance_then_one_cross_channel_loyalty_permit(
    owned,
    private_source,
    monkeypatch,
):
    from apps.loyalty.models import JoinInvitation
    from apps.reservations import telegram_stay_financial as financial

    for _ in range(3):
        accepted(owned)
    paid = fixtures.paid_record(owned)
    data = source_tests.payload(owned, operation="next", event_id=str(paid[2].pk))
    with transaction.atomic():
        value = source.snapshot(data)
    monkeypatch.setattr("apps.loyalty.invitation_api.ready", lambda: True)
    result = journey.next_packet(value, data)
    assert result["job"]["purpose"] == "payment_success" and not JoinInvitation.objects.exists()
    row = TelegramStayMessage.objects.get(job_key=result["job"]["job_key"])
    journey.import_evidence(row, proof(row))
    invitation_packet = journey.next_packet(value, data)
    assert invitation_packet["job"]["purpose"] == "loyalty_invitation"
    invitation = JoinInvitation.objects.get()
    assert invitation.channel == "telegram" and invitation.state == "unknown"
    # Retrying the same source packet never mints a second cross-channel permit.
    assert journey.next_packet(value, data)["job"]["job_key"] == invitation_packet["job"]["job_key"]
    assert JoinInvitation.objects.count() == 1
    row = TelegramStayMessage.objects.get(job_key=invitation_packet["job"]["job_key"])
    journey.import_evidence(row, proof(row))
    invitation.refresh_from_db()
    assert invitation.state == "accepted" and invitation.provider_id
    assert financial.financial(value, paid[1], paid[2])["decision"] == "done"
    type(paid[1]).objects.filter(pk=paid[1].pk).update(amount_sar="999.00")
    paid[1].refresh_from_db()
    assert financial.financial(value, paid[1], paid[2])["reason"] == "current_paid_core_required"


def test_review_template_has_its_own_parser_and_source_profile_evidence(
    owned, private_source, monkeypatch
):
    from apps.reservations import telegram_stay_reviews as reviews

    _, stay, _ = owned
    stay.starts_at = timezone.now() - timedelta(hours=5)
    stay.ends_at = timezone.now() - timedelta(hours=2)
    body = "*Welcome {guest_name}*\n{listing_name}\n{review_url}"
    policy = {
        "contract": "tg39_stay_review_policy_v1",
        "available": True,
        "decision": "send",
        "step": 1,
        "purpose": "review_request",
        "template_key": "website_review_1_en",
        "template_body": body,
        "template_sha256": render.sha(body),
        "not_before": (stay.ends_at + timedelta(minutes=1)).isoformat(),
        "expires_at": (stay.ends_at + timedelta(days=1)).isoformat(),
        "google_profile_id": "12345",
        "review_url": "https://g.page/r/SYNTHETIC/review",
        "profile_evidence": {
            "kind": "owner_maintained_approved_mapping",
            "source": "public.website_review_properties",
            "row_sha256": "a" * 64,
            "read_at": timezone.now().isoformat(),
            "approval_at": None,
        },
    }
    due, expiry = reviews.checked(policy, stay)
    value = packet(owned)
    row = journey.plan(value, "review_request", 11, due, expiry)
    stay.guest_name = "<script>guest</script> *literal*"
    monkeypatch.setattr(bridge, "read", lambda *a, **k: policy)
    content, sha = reviews.content(row, stay)
    assert sha == render.sha(body) and "<script>" not in content["body_html"]
    assert "*literal*" in content["body_html"] and "{review_url}" not in content["body_html"]
    assert "<b>Welcome " in content["body_html"]
    policy["profile_evidence"]["kind"] = "caller_assertion"
    with pytest.raises(authority.Denied):
        reviews.checked(policy, stay)


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


@pytest.fixture
def admin_proof(owned, settings, monkeypatch):
    from django.contrib.auth import get_user_model
    from django.test import RequestFactory

    from apps.reservations import telegram_stay_activation as service

    settings.TELEGRAM_ACTIVATION_ENABLED = True
    user = get_user_model().objects.create_user(
        "synthetic-tg-owner",
        email=settings.OPERATIONS_OWNER_EMAIL,
        is_staff=True,
        is_superuser=True,
    )
    request = RequestFactory().post(
        "/private-stay-activation/",
        {
            "phone": owned[1].guest_phone,
            "evidence_ref": "synthetic-independent-proof",
            "confirmed": "on",
        },
    )
    request.user, request.session = user, {}
    calls = []

    class FakeBackend:
        authority_context = None

        def prepare(self, key):
            pass

        def rpc(self, name, values):
            action = "approve_phone" if name.endswith("approve_phone") else "issue_invitation"
            envelope = authority.mint(self.authority_context, action, values["p_key"])
            envelope["contract"] = "tg39_activation_readback_v1"
            approved = authority.authorize(envelope)
            calls.append(action)
            if action == "approve_phone":
                assert approved["phone"] == owned[1].guest_phone.lstrip("+")
                assert approved["evidence_ref"].startswith("admin_verified:")
                with pytest.raises(authority.Denied):
                    authority.authorize(envelope)
                return {"action": "approved"}
            assert "phone" not in approved
            return {
                "action": "issued",
                "url": "https://t.me/LuxurySmartApartmentsBot?start="
                + ("h" if owned[0] == "07" else "d")
                + "_"
                + "a" * 48,
                "expires_at": (timezone.now() + timedelta(hours=2)).isoformat(),
            }

        def close(self):
            pass

    monkeypatch.setattr(service, "Backend", FakeBackend)
    return service, request, calls


def test_admin_independent_phone_proof_is_once_and_never_stores_link_or_phone(owned, admin_proof):
    from apps.reservations.telegram_stay_models import TelegramStayActivation

    service, request, calls = admin_proof
    result = service.issue(request, owned[1], owned[0])
    assert result["state"] == "issued" and result["url"]
    assert service.issue(request, owned[1], owned[0])["state"] == "active"
    assert calls == ["approve_phone", "issue_invitation"]
    row = TelegramStayActivation.objects.get()
    assert row.approve_authorized_at and row.issue_authorized_at
    stored = str(list(TelegramStayActivation.objects.values()))
    assert owned[1].guest_phone not in stored and result["url"] not in stored


def test_admin_phone_must_match_this_stay_and_confirmation_is_required(owned, admin_proof):
    from apps.reservations.telegram_stay_models import TelegramStayActivation

    service, request, calls = admin_proof
    request.POST = {"phone": "+12025550999", "evidence_ref": "synthetic-proof", "confirmed": "on"}
    assert service.issue(request, owned[1], owned[0])["state"] == "unverified"
    request.POST = {"phone": owned[1].guest_phone, "evidence_ref": "synthetic-proof"}
    assert service.issue(request, owned[1], owned[0])["state"] == "unverified"
    assert not calls and not TelegramStayActivation.objects.exists()
