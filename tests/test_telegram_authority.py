"""Real Django authority/bridge with synthetic data and mocked network only."""

import json
import uuid
from datetime import timedelta

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.loyalty.invitation_auth import active_credential
from apps.loyalty.models import InvitationAPICredential
from apps.reservations import telegram_authority as authority
from apps.reservations import telegram_backend as backend
from apps.reservations.models import TelegramActivation
from tests import test_loyalty_invitation_api as loyalty
from tests import test_telegram_activation as activation

pytestmark = pytest.mark.django_db
rig = activation.rig
auth = loyalty.auth
PATH = "/integrations/telegram39/activation-authority/"


def post(envelope, **kwargs):
    data = dict(envelope, contract="tg39_activation_readback_v1")
    return Client(enforce_csrf_checks=True).post(
        PATH, data, content_type="application/json", secure=True, **(loyalty.HEADERS | kwargs)
    )


@pytest.fixture
def proof(rig, auth):
    phone = rig.booking.booking_intent.guest_phone
    row = TelegramActivation.objects.create(
        reservation=rig.booking,
        actor=rig.user,
        proof_kind="website_verified",
        phone_digest=authority.phone_digest(phone),
        evidence_ref="verified-phone:test",
    )
    return row, authority.booking_scope(rig.request, rig.booking, row=row, phone=phone)


def test_real_readback_consumes_each_write_before_response_once(proof, rig):
    row, scope = proof
    approve = authority.mint(scope, "approve_phone", str(rig.booking.hostaway_reservation_id))
    response = post(approve)
    assert response.status_code == 200
    value = response.json()
    assert value["phone"] == rig.booking.booking_intent.guest_phone[1:]
    assert value["evidence_ref"] == "website_verified:" + str(row.proof_id)
    assert value["phone_proof_verified"] is True
    assert "no-store" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "no-referrer"
    assert post(approve).status_code == 403
    # A new nonce cannot bypass a consumed action, and no private payload is stored.
    again = authority.mint(scope, "approve_phone", scope["key"])
    assert again["request_id"] == approve["request_id"]
    assert post(again).status_code == 403
    issue = authority.mint(scope, "issue_invitation", scope["key"])
    result = post(issue)
    assert result.status_code == 200 and "phone" not in result.json()
    assert "evidence_ref" not in result.json()
    assert post(issue).status_code == 403
    row.refresh_from_db()
    assert row.approve_authorized_at and row.issue_authorized_at
    stored = str(list(TelegramActivation.objects.values()))
    assert scope["phone"] not in stored and approve["capability"] not in stored


def test_issue_cannot_precede_phone_authority(proof):
    _, scope = proof
    assert post(authority.mint(scope, "issue_invitation", scope["key"])).status_code == 403


@pytest.mark.parametrize(
    "case",
    [
        "wrong_action",
        "wrong_id",
        "tamper",
        "expired",
        "future",
        "key",
        "phone",
        "binding_changed",
        "owner_changed",
        "user_disabled",
        "proof_changed",
        "state",
        "flag_closed",
        "extra",
        "credential_revoked",
    ],
)
def test_readback_fails_closed_for_invalid_or_changed_authority(proof, rig, settings, case):
    row, scope = proof
    envelope = authority.mint(scope, "approve_phone", scope["key"])
    if case == "wrong_action":
        envelope["action"] = "issue_invitation"
    elif case == "wrong_id":
        envelope["request_id"] = str(uuid.uuid4())
    elif case == "tamper":
        envelope["capability"] += "x"
    elif case in {"expired", "future", "key", "phone"}:
        value = signing.loads(envelope["capability"], salt=authority.SALT)
        if case == "expired":
            value["expires"] = int(timezone.now().timestamp()) - 1
        elif case == "future":
            value["issued"] = int(timezone.now().timestamp()) + 1
        elif case == "key":
            value["key"] = "70012346"
        else:
            value["scope"]["phone"] = "+12025550999"
        envelope["capability"] = signing.dumps(value, salt=authority.SALT)
    elif case == "binding_changed":
        VerifiedPhone.objects.filter(user=rig.user).update(version=uuid.uuid4())
    elif case == "owner_changed":
        rig.booking.booking_intent.customer = None
        rig.booking.booking_intent.save(update_fields=["customer"])
    elif case == "user_disabled":
        get_user_model().objects.filter(pk=rig.user.pk).update(is_active=False)
    elif case == "proof_changed":
        TelegramActivation.objects.filter(pk=row.pk).update(proof_id=uuid.uuid4())
    elif case == "state":
        TelegramActivation.objects.filter(pk=row.pk).update(state="uncertain")
    elif case == "flag_closed":
        settings.TELEGRAM_ACTIVATION_ENABLED = False
    elif case == "extra":
        envelope["phone"] = "+12025550123"
    elif case == "credential_revoked":
        InvitationAPICredential.objects.update(revoked_at=timezone.now())
    response = post(envelope)
    assert response.status_code in {401, 403}
    row.refresh_from_db()
    assert row.approve_authorized_at is None
    assert scope["phone"].encode() not in response.content


def test_bearer_alone_and_public_reference_never_approve(proof):
    _, scope = proof
    unauthorized = Client().post(PATH, {}, content_type="application/json", secure=True)
    assert unauthorized.status_code == 401
    assert unauthorized["Referrer-Policy"] == "no-referrer"
    assert "no-store" in unauthorized["Cache-Control"]
    assert (
        post(
            {"request_id": str(uuid.uuid4()), "action": "approve_phone", "capability": "x"}
        ).status_code
        == 403
    )
    with pytest.raises(authority.Denied):
        authority.mint({"reservation": scope["reservation"]}, "approve_phone", scope["key"])


def test_loyalty_binding_scope_has_no_phone_or_write_authority(proof, settings):
    settings.TELEGRAM_LOYALTY_INVITATIONS_ENABLED = True
    credential = InvitationAPICredential.objects.get()
    context = active_credential.set(str(credential.pk))
    try:
        scope = authority.loyalty_scope("70012345")
        read = authority.mint(scope, "binding", "70012345")
        result = post(read)
        assert result.status_code == 200 and "phone" not in result.json()
        assert post(authority.mint(scope, "prepare", "70012345")).status_code == 403
        with pytest.raises(authority.Denied):
            authority.mint(scope, "approve_phone", "70012345")
    finally:
        active_credential.reset(context)
    assert active_credential.get() is None


def test_guest_or_bare_admin_page_is_not_an_operations_proof(rig):
    with pytest.raises(authority.Denied):
        authority.booking_scope(rig.request, rig.booking, admin=True)


def test_full_bridge_round_trip_uses_local_readback_and_no_private_rest(proof, rig, monkeypatch):
    _, scope = proof
    # rig replaces Backend for UI tests. Use the saved concrete class instead.
    concrete = REAL_BACKEND
    original = httpx.Client
    actions = []

    def handle(request):
        assert str(request.url) == backend.BASE
        assert "authorization" not in request.headers and not request.url.query
        envelope = json.loads(request.content)
        verified = post(envelope)
        assert verified.status_code == 200
        actions.append(envelope["action"])
        if envelope["action"] == "health":
            value = {
                "contract": "tg20_guest_journey_v1",
                "ready": True,
                "dispatch_enabled": True,
                "cutover_configured": True,
                "components": dict.fromkeys(
                    ("s01", "reminders", "reviews", "contacts", "loyalty"), True
                ),
            }
        elif envelope["action"] == "prepare":
            value = {
                "booking_available": True,
                "source_fresh": True,
                "bound": False,
                "invitation_state": "none",
            }
        elif envelope["action"] == "approve_phone":
            value = {"action": "approved", "reservation_key": scope["key"]}
        else:
            value = {
                "action": "issued",
                "url": activation.URL,
                "expires_at": (timezone.now() + timedelta(hours=2)).isoformat(),
            }
        return httpx.Response(
            200,
            json={
                "contract": "tg39_activation_bridge_result_v1",
                "request_id": envelope["request_id"],
                "action": envelope["action"],
                "result": value,
            },
        )

    monkeypatch.setattr(
        backend.httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(handle))
    )
    client = concrete()
    client.authority_context = scope
    try:
        client.prepare(scope["key"])
        approved = client.rpc(
            "tg20_s01_approve_phone",
            {
                "p_key": scope["key"],
                "p_phone": scope["phone"][1:],
                "p_evidence_ref": scope["evidence_ref"],
            },
        )
        assert approved["action"] == "approved"
        issued = client.rpc("tg20_s01_issue_invitation", {"p_key": scope["key"]})
        assert backend.checked_invitation(issued)[0] == activation.URL
    finally:
        client.close()
    assert actions == ["health", "prepare", "approve_phone", "issue_invitation"]


REAL_BACKEND = backend.Backend
