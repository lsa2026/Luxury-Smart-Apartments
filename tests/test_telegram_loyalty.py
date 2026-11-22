"""Shared registry with mocked Telegram binding; no sends or new credentials."""

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import VerifiedPhone
from apps.loyalty import telegram_invitations
from apps.loyalty.models import JoinInvitation
from tests import test_loyalty_invitation_api as legacy

pytestmark = pytest.mark.django_db
auth = legacy.auth
rig = legacy.rig


@pytest.fixture
def target(settings, monkeypatch):
    settings.TELEGRAM_LOYALTY_INVITATIONS_ENABLED = True
    value = {"chat": "12345678"}

    class Backend:
        def binding(self, key, phone):
            assert key == "70012345" and phone.startswith("+")
            return value["chat"]

        def close(self):
            pass

    monkeypatch.setattr(telegram_invitations, "Backend", Backend)
    return value


def test_telegram_claim_preflight_record_once_and_cross_channel_suppression(rig, target):
    result = legacy.claim(channel="telegram")
    assert result["channel"] == "telegram" and result["provider_receipt_lookup"] is False
    assert result["recipient_chat_id"] == "12345678" and result["send_allowed"] is False
    permitted = legacy.preflight(result).json()
    assert permitted["send_allowed"] is True and permitted["recipient_chat_id"] == "12345678"
    assert legacy.preflight(result).json()["send_allowed"] is False
    assert legacy.report(result, "accepted", "123").status_code == 200
    assert legacy.claim(event_id="second-event")["decision"] == "duplicate"
    assert JoinInvitation.objects.count() == 1


def test_whatsapp_claim_suppresses_telegram(rig, target):
    legacy.claim()
    result = legacy.claim(channel="telegram", event_id="new-telegram-event")
    assert result["decision"] == "duplicate" and not result["send_allowed"]


@pytest.mark.parametrize("status", ["delivered", "read", "failed"])
def test_no_whatsapp_receipt_states_for_telegram(rig, target, status):
    result = legacy.claim(channel="telegram")
    legacy.preflight(result)
    assert legacy.report(result, status, "123").status_code == 400
    assert JoinInvitation.objects.get().state == "unknown"


def test_unknown_never_allows_retry(rig, target):
    result = legacy.claim(channel="telegram")
    legacy.preflight(result)
    assert legacy.report(result, "unknown", "").status_code == 200
    assert legacy.claim(channel="telegram")["decision"] == "duplicate"
    assert legacy.preflight(result).json()["send_allowed"] is False


def test_binding_change_fails_before_permit(rig, target):
    result = legacy.claim(channel="telegram")
    target["chat"] = "87654321"
    assert legacy.preflight(result).status_code == 409
    assert JoinInvitation.objects.get().dispatched_at is None


def test_disabled_and_invalid_channels_cannot_claim(rig, target, settings):
    settings.TELEGRAM_LOYALTY_INVITATIONS_ENABLED = False
    assert legacy.post("claim", legacy.data(channel="telegram")).status_code == 409
    assert not JoinInvitation.objects.exists()
    assert legacy.post("claim", legacy.data(channel="email")).status_code == 400


def test_binding_gate_off_without_network(settings):
    settings.TELEGRAM_LOYALTY_INVITATIONS_ENABLED = False
    with pytest.raises(ValidationError):
        telegram_invitations.target("hostaway:70012345", "+12025550123")


def test_existing_member_is_suppressed_across_telegram(rig, target):
    account = legacy.user("telegram-existing-member")
    legacy.join(account)
    VerifiedPhone.objects.create(user=account, phone="+12025550123")
    result = legacy.claim(channel="telegram")
    assert result["decision"] == "member" and not result["send_allowed"]
    assert not JoinInvitation.objects.exists()


def test_same_event_cannot_change_channel(rig, target):
    legacy.claim()
    result = legacy.post("claim", legacy.data(channel="telegram"))
    assert result.status_code == 409
    assert JoinInvitation.objects.get().channel == "whatsapp"
