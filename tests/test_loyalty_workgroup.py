"""Shared invitation reservation for a manual workgroup copy, never guest delivery."""

import pytest

from apps.loyalty.models import JoinInvitation, Ledger, Member
from tests import test_loyalty_invitation_api as legacy

pytestmark = pytest.mark.django_db
auth = legacy.auth
rig = legacy.rig


@pytest.fixture(autouse=True)
def group(settings):
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = True
    settings.TELEGRAM_LOYALTY_INVITATIONS_ENABLED = False


def test_claim_preflight_handoff_once_without_chat_binding(rig):
    value = legacy.claim(channel="workgroup_manual")
    assert value["channel"] == "workgroup_manual" and not value["handoff_allowed"]
    assert not value["send_allowed"] and not value["guest_accepted"]
    permit = legacy.preflight(value).json()
    assert permit["handoff_allowed"] and not permit["send_allowed"]
    assert "handoff_expires_at" in permit and "send_expires_at" not in permit
    assert "recipient_chat_id" not in permit
    assert not legacy.preflight(value).json()["handoff_allowed"]
    reply = legacy.report(value, "handed_off", "123").json()
    assert reply["code"] == "ok" and reply["status"] == "handed_off"
    assert not any(reply[k] for k in ("guest_accepted", "delivered", "read", "send_allowed"))
    assert legacy.claim(channel="workgroup_manual", event_id="later")["decision"] == "duplicate"
    assert not Member.objects.exists() and not Ledger.objects.exists()


@pytest.mark.parametrize("status", ["accepted", "delivered", "read", "failed"])
def test_group_cannot_record_guest_delivery(rig, status):
    value = legacy.claim(channel="workgroup_manual")
    legacy.preflight(value)
    assert legacy.report(value, status, "123").status_code == 400
    assert JoinInvitation.objects.get().state == "unknown"


def test_missing_phone_and_unknown_never_invent_binding_or_allow_retry(rig):
    value = legacy.claim(channel="workgroup_manual", phone="")
    assert value["recipient_phone"] == ""
    assert legacy.preflight(value, phone="").json()["handoff_allowed"]
    assert legacy.report(value, "unknown", "").status_code == 200
    assert not legacy.preflight(value, phone="").json()["handoff_allowed"]


def test_direct_n8n_guest_claims_closed_and_flag_pause_does_not_release(rig, settings):
    assert legacy.post("claim", legacy.data(channel="telegram")).status_code == 409
    assert legacy.post("claim", legacy.data()).status_code == 409
    value = legacy.claim(channel="workgroup_manual")
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
    assert legacy.preflight(value).status_code == 503
    assert JoinInvitation.objects.get().dispatched_at is None
