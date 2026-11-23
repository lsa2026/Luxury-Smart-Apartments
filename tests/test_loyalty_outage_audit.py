"""Offline rollout fences derived from the 2026-11-23 outage investigation."""

import pytest

from apps.accounts import phone_auth
from apps.accounts.models import VerifiedPhone
from apps.loyalty import whatsapp_notices
from apps.loyalty.models import GuestEvent, MovementNotice
from tests import test_loyalty_stage8 as stage8
from tests import test_loyalty_whatsapp_notices as legacy_notices

pytestmark = pytest.mark.django_db
local_program = stage8.local_program
rig = legacy_notices.rig


def test_unconfigured_cutover_cannot_replay_email_only_join(
    local_program,
    settings,
):
    settings.TELEGRAM_WORKGROUP_ONLY_POLICY = True
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = True
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = False
    guest = stage8.user("audit-email-only")
    member = stage8.join(guest)
    assert member.status == "active"
    assert GuestEvent.objects.filter(member=member, kind=GuestEvent.Kind.JOIN).count() == 1
    assert not VerifiedPhone.objects.filter(user=guest).exists()
    assert not MovementNotice.objects.exists()


def test_workgroup_flag_alone_cannot_enable_phone_verification(settings):
    settings.ACCOUNT_WHATSAPP_AUTH_ENABLED = True
    settings.ACCOUNT_WHATSAPP_AUTH_ISOLATED = True
    settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC = True
    settings.TELEGRAM_WORKGROUP_ONLY_POLICY = True
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = True
    settings.ULTRAMSG_ENABLED = False
    assert not phone_auth.ready()


def test_old_notice_is_retired_under_workgroup_policy_even_with_legacy_flags(
    rig,
    settings,
):
    settings.TELEGRAM_WORKGROUP_ONLY_POLICY = False
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = False
    rig.gift("audit-old-provider-fence")
    notice = MovementNotice.objects.order_by("created_at", "pk").first()
    settings.TELEGRAM_WORKGROUP_ONLY_POLICY = True
    settings.TELEGRAM_WORKGROUP_HANDOFF_ENABLED = True
    whatsapp_notices.deliver_movement_notice(notice.pk)
    assert rig.provider.send_image.call_count == 0
    # All provider calls are mocks supplied by the existing fixture.
    notice.refresh_from_db()
    assert notice.status == "skipped"
