"""Real loyalty12 outbox through site01 authority/receipt; synthetic sender only."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.loyalty import phone_gifts, whatsapp_notices
from apps.loyalty.models import Ledger
from apps.reservations import telegram_authority as authority
from apps.reservations import telegram_website_workgroup as service
from apps.reservations.telegram_stay_models import TelegramWebsiteHandoff
from tests import test_loyalty_workgroup_notices as loyalty
from tests.test_website_workgroup import proof

pytestmark = pytest.mark.django_db
local_program = loyalty.local_program
policy = loyalty.policy


@pytest.fixture(autouse=True)
def clocks(policy, monkeypatch):
    for field in ("created_at", "next_probe_at"):
        monkeypatch.setattr(
            TelegramWebsiteHandoff._meta.get_field(field), "_get_default", lambda: timezone.now()
        )


@pytest.fixture
def bridge(monkeypatch):
    calls, packets = [], []

    class Backend:
        authority_context = None

        def call(self, action, key):
            envelope = authority.mint(self.authority_context, action, key)
            result = authority.authorize(dict(envelope, contract="tg39_activation_readback_v1"))
            calls.append(action)
            packets.append(result["packet"])
            return proof(TelegramWebsiteHandoff.objects.get(pk=key))

        def close(self):
            pass

    monkeypatch.setattr(service, "Backend", Backend)
    return calls, packets


def test_join_without_phone_runs_full_source_receipt_callback_once(bridge, settings):
    settings.TELEGRAM_WORKGROUP_CUTOVER_AT = (timezone.now() + timedelta(days=1)).isoformat()
    _, _, notice = loyalty.joined()
    whatsapp_notices.deliver_movement_notice(notice.pk)
    notice.refresh_from_db()
    row = TelegramWebsiteHandoff.objects.get()
    packet = bridge[1][0]
    assert notice.status == "handed_off" and row.state == "handed_off"
    assert packet["purpose"] == "loyalty_join" and packet["guest_phone_e164"] is None
    assert packet["asset_base64"] and packet["caption_html"]
    assert not VerifiedPhone.objects.exists() and not Ledger.objects.exists()
    whatsapp_notices.deliver_movement_notice(notice.pk)
    assert bridge[0] == ["website_handoff"]
    stored = str(list(TelegramWebsiteHandoff.objects.values()))
    assert "caption_html" not in stored and "guest_phone_e164" not in stored


def test_gift_uses_saved_receipt_and_real_callback_without_points(bridge):
    member, key = loyalty.gift()
    phone_gifts.deliver_invitation(member.pk, key)
    member.refresh_from_db()
    packet = bridge[1][0]
    assert packet["source_kind"] == "loyalty_gift"
    assert packet["purpose"] == "loyalty_gift_invitation"
    assert member.command_receipts[key]["result"]["status"] == "handed_off"
    assert not Ledger.objects.exists()
    phone_gifts.deliver_invitation(member.pk, key)
    assert bridge[0] == ["website_handoff"]


def test_historical_unknown_import_does_not_reauthorize_withdrawn_member(
    monkeypatch, settings, local_program
):
    calls = []
    accepted_at = timezone.now().isoformat()

    class Backend:
        authority_context = None

        def call(self, action, key):
            calls.append(action)
            value = service.authorize(self.authority_context, action, key, {})["packet"]
            if action == "website_handoff":
                raise service.Unavailable
            row = TelegramWebsiteHandoff.objects.get(pk=key)
            assert set(value) == {"contract", *service.metadata(row)}
            return proof(row, accepted_at=accepted_at)

        def close(self):
            pass

    monkeypatch.setattr(service, "Backend", Backend)
    _, member, notice = loyalty.joined()
    whatsapp_notices.deliver_movement_notice(notice.pk)
    notice.refresh_from_db()
    assert notice.status == "handoff_hold"
    member.status = "withdrawn"
    member.save(update_fields=["status"])
    local_program[0] += timedelta(days=1)
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
    service.recover()
    notice.refresh_from_db()
    assert notice.status == "handed_off" and calls == ["website_handoff", "website_handoff_status"]
    assert not Ledger.objects.exists() and not VerifiedPhone.objects.exists()


def test_missing_loyalty_cutover_cannot_borrow_ordinary_cutover(settings, bridge):
    _, _, notice = loyalty.joined()
    settings.LOYALTY_WORKGROUP_CUTOVER_AT = ""
    settings.TELEGRAM_WORKGROUP_CUTOVER_AT = (timezone.now() - timedelta(days=1)).isoformat()
    whatsapp_notices.deliver_movement_notice(notice.pk)
    assert not bridge[0] and not TelegramWebsiteHandoff.objects.exists()
