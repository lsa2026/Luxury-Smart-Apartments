"""Synthetic end-to-end source/outbox contracts; no live broker or provider."""

from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.accounts.models import VerifiedPhone, profile_for
from apps.loyalty import phone_gifts, whatsapp_notices
from apps.loyalty import workgroup_notices as group
from apps.loyalty.models import GuestEvent, Ledger, MovementNotice
from apps.loyalty.services import reconcile
from apps.reservations import telegram_website_workgroup as transport
from apps.reservations.telegram_authority import Denied
from tests import test_loyalty_stage8 as base

pytestmark = pytest.mark.django_db
local_program = base.local_program


@pytest.fixture(autouse=True)
def policy(settings, monkeypatch, local_program):
    settings.TELEGRAM_WORKGROUP_ONLY_POLICY = True
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = True
    settings.LOYALTY_WORKGROUP_CUTOVER_AT = (timezone.now() - timedelta(seconds=1)).isoformat()
    settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "loyalty-terms-2026-10-18-v5"
    settings.LOYALTY_TERMS_REFERENCE = "/ar/legal/rewards/2026-10-18-v5/"
    settings.SITE_BASE_URL = "https://example.invalid"
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = False
    settings.ULTRAMSG_ENABLED = False
    monkeypatch.setattr(whatsapp_notices, "_dispatch", Mock())
    monkeypatch.setattr(phone_gifts, "_dispatch", Mock())
    monkeypatch.setattr(
        whatsapp_notices, "UltraMsgClient", Mock(side_effect=AssertionError("legacy send"))
    )
    monkeypatch.setattr(
        phone_gifts, "UltraMsgClient", Mock(side_effect=AssertionError("legacy send"))
    )


def joined():
    guest = base.user("workgroup-new")
    member = base.join(guest)
    return guest, member, MovementNotice.objects.get(event__kind=GuestEvent.Kind.JOIN)


def fake_send(calls):
    def send(kind, source_id):
        calls.append((kind, source_id))
        if kind == "loyalty_notice":
            group.source_notice(source_id)
            group.finish_notice(source_id, "123")
        else:
            group.source_gift(source_id)
            group.finish_gift(source_id, "124")
        return "handed_off"

    return send


def test_email_only_join_is_queued_and_forwarded_once_without_phone_proof(monkeypatch):
    guest, member, notice = joined()
    profile = profile_for(guest)
    profile.phone = "+12025550123"
    profile.save()
    before = (member.pk, member.number, Ledger.objects.count())
    packet = group.source_notice(notice.pk)
    assert packet[3] == profile.phone and packet[-1] == "loyalty_join"
    assert packet[5]["phone_verified"] is False
    calls = []
    monkeypatch.setattr(transport, "deliver", fake_send(calls))
    whatsapp_notices.deliver_movement_notice(notice.pk)
    whatsapp_notices.deliver_movement_notice(notice.pk)
    notice.refresh_from_db()
    assert calls == [("loyalty_notice", str(notice.pk))]
    assert notice.status == "handed_off" and notice.attempts == 1
    assert not VerifiedPhone.objects.exists()
    assert before == (member.pk, member.number, Ledger.objects.count())


def test_unknown_outcome_is_held_and_never_posted_again(monkeypatch):
    _, _, notice = joined()
    sender = Mock(side_effect=TimeoutError())
    monkeypatch.setattr(transport, "deliver", sender)
    whatsapp_notices.deliver_movement_notice(notice.pk)
    whatsapp_notices.deliver_movement_notice(notice.pk)
    notice.refresh_from_db()
    assert notice.status == "handoff_hold" and sender.call_count == 1
    group.finish_notice(notice.pk, "125")
    notice.refresh_from_db()
    assert notice.status == "handed_off"


@pytest.mark.parametrize("cutoff", ["", "2026-10-11T00:00:00+00:00", "2026-99-99T00:00:00Z"])
def test_missing_or_future_cutoff_does_not_replay_old_join(settings, cutoff):
    settings.LOYALTY_WORKGROUP_CUTOVER_AT = cutoff
    guest = base.user("old-join")
    base.join(guest)
    assert not MovementNotice.objects.exists()


def test_source_time_not_new_outbox_time_controls_admission(settings):
    _, _, notice = joined()
    # Admission follows the immutable source time, even if an outbox is recent.
    settings.LOYALTY_WORKGROUP_CUTOVER_AT = (timezone.now() + timedelta(seconds=1)).isoformat()
    with pytest.raises(Denied):
        group.source_notice(notice.pk)


def test_legacy_queued_notice_cannot_use_guest_sender_or_become_new_copy(monkeypatch):
    _, _, notice = joined()
    notice.template_version = "lifecycle-whatsapp-v1"
    notice.save(update_fields=["template_version"])
    sender = Mock()
    monkeypatch.setattr(transport, "deliver", sender)
    whatsapp_notices.deliver_movement_notice(notice.pk)
    notice.refresh_from_db()
    assert notice.status == "skipped" and not sender.called
    assert notice.error_code == "legacy_transport_retired"


def test_disabled_group_preserves_new_outbox_without_fallback(settings, monkeypatch):
    _, _, notice = joined()
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
    sender = Mock()
    monkeypatch.setattr(transport, "deliver", sender)
    whatsapp_notices.deliver_movement_notice(notice.pk)
    notice.refresh_from_db()
    assert notice.status == "queued" and notice.attempts == 0
    assert not sender.called


def test_new_movement_uses_posted_ledger_and_never_adds_another_credit(
    monkeypatch, settings, local_program
):
    settings.LOYALTY_WORKGROUP_CUTOVER_AT = (timezone.now() - timedelta(days=1)).isoformat()
    local_program[0] -= timedelta(hours=2)
    guest, member, _ = joined()
    local_program[0] += timedelta(hours=2)
    # Model defaults bind the original clock at import; align this synthetic ledger.
    monkeypatch.setattr(Ledger._meta.get_field("occurred_at"), "_get_default", timezone.now)
    result = reconcile(base.booking(guest).pk)
    assert result["movements"] > 0, result
    notice = MovementNotice.objects.get(ledger__isnull=False)
    before = list(Ledger.objects.values_list("pk", "credit", "debit"))
    calls = []
    monkeypatch.setattr(transport, "deliver", fake_send(calls))
    assert group.source_notice(notice.pk)[-1] == "loyalty_movement"
    whatsapp_notices.deliver_movement_notice(notice.pk)
    assert before == list(Ledger.objects.values_list("pk", "credit", "debit"))


def gift():
    guest, member, _ = joined()
    member.first_completed_at = timezone.now() - timedelta(days=1)
    member.invitation_token = "synthetic-inviter"
    member.save(update_fields=["first_completed_at", "invitation_token"])
    key = "gift-invite:synthetic-once"
    phone_gifts.queue_invitation(guest, phone="+12025550124", language="ar", key=key, consent=True)
    return member, key


def test_explicit_gift_invitation_goes_to_workgroup_once_without_credit(monkeypatch):
    member, key = gift()
    calls = []
    monkeypatch.setattr(transport, "deliver", fake_send(calls))
    phone_gifts.deliver_invitation(member.pk, key)
    phone_gifts.deliver_invitation(member.pk, key)
    member.refresh_from_db()
    assert calls == [("loyalty_gift", f"{member.pk}:{key}")]
    assert member.command_receipts[key]["result"]["status"] == "handed_off"
    assert not Ledger.objects.exists()


def test_existing_member_becoming_recipient_blocks_unsent_invitation():
    member, key = gift()
    recipient = base.user("gift-now-member")
    base.join(recipient)
    VerifiedPhone.objects.create(user=recipient, phone="+12025550124")
    with pytest.raises(Denied):
        group.source_gift(f"{member.pk}:{key}")


def test_withdrawn_member_join_source_is_not_forwarded():
    _, member, notice = joined()
    member.status = "withdrawn"
    member.save(update_fields=["status"])
    with pytest.raises(Denied):
        group.source_notice(notice.pk)


def test_workgroup_gift_never_falls_back_after_manual_policy_is_disabled(settings):
    member, key = gift()
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
    settings.TELEGRAM_WORKGROUP_ONLY_POLICY = False
    settings.ULTRAMSG_ENABLED = True
    phone_gifts.deliver_invitation(member.pk, key)
    member.refresh_from_db()
    assert member.command_receipts[key]["result"]["status"] == "queued"


def test_group_policy_blocks_loyalty_email_queue_and_pending_email_resolution(settings):
    from types import SimpleNamespace

    from apps.loyalty import notifications, welcome
    from apps.notifications.services.email import EmailProviderError

    settings.EMAIL_DELIVERY_ENABLED = True
    settings.LOYALTY_MESSAGES_ENABLED = True
    assert not notifications.messages_enabled()
    assert not welcome.enabled()
    assert notifications.dispatch_due() == {"status": "disabled", "queued": 0}
    for source in ("loyalty_welcome", "loyalty_ledger", "loyalty_invitation"):
        with pytest.raises(EmailProviderError):
            notifications.resolve_recipient(SimpleNamespace(recipient_source=source))


def test_direct_preview_is_blocked_before_owner_lookup():
    from django.core.exceptions import ValidationError

    from apps.loyalty.message_previews import send_catalogue

    with pytest.raises(ValidationError):
        send_catalogue(batch="synthetic", language="ar")


def test_workgroup_notice_is_preserved_if_manual_policy_is_temporarily_disabled(settings):
    _, _, notice = joined()
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
    settings.TELEGRAM_WORKGROUP_ONLY_POLICY = False
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = True
    settings.ULTRAMSG_ENABLED = True
    whatsapp_notices.deliver_movement_notice(notice.pk)
    notice.refresh_from_db()
    assert notice.status == "queued" and notice.attempts == 0
