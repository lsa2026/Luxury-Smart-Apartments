"""Offline fake-provider tests. No real guest, network, broker or provider secrets."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from django.utils.translation import override

from apps.accounts.models import VerifiedPhone, profile_for
from apps.loyalty import whatsapp_notices as notices
from apps.loyalty.models import Ledger, MovementNotice
from apps.loyalty.services import balance, reconcile, transfer, withdraw
from scheduler_runtime.schedule import build_beat_schedule
from tests import test_loyalty_stage9
from tests.test_loyalty_stage8 import booking
from tests.test_loyalty_stage9 import funded_pair, login, owner

pytestmark = pytest.mark.django_db
notice_clock = test_loyalty_stage9.ui


@pytest.fixture
def rig(settings, monkeypatch, django_capture_on_commit_callbacks):
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = False
    a, b, am, bm = funded_pair()  # Historical posting, intentionally not notified.
    bindings = [
        VerifiedPhone.objects.create(user=u, phone=p)
        for u, p in [
            (a, "+15550001111"),
            (b, "+15550002222"),
        ]
    ]
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = True
    settings.LOYALTY_MESSAGES_ENABLED = False  # Do not accidentally enable the email campaign.
    settings.ULTRAMSG_ENABLED = True
    settings.ULTRAMSG_INSTANCE_ID = "synthetic-only"
    settings.ULTRAMSG_TOKEN = "synthetic-only"
    settings.SITE_BASE_URL = "https://example.invalid"
    provider = Mock()
    provider.http.get.return_value = SimpleNamespace(
        status_code=200,
        json=lambda: {"status": {"accountStatus": {"status": "authenticated"}}},
    )
    provider.send_image.return_value = {"sent": True, "id": "synthetic-message"}
    client = Mock()
    client.__enter__ = Mock(return_value=provider)
    client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(notices, "UltraMsgClient", Mock(return_value=client))
    dispatched = []
    original_dispatch = notices._dispatch
    monkeypatch.setattr(notices, "_dispatch", lambda key: dispatched.append(str(key)))

    def gift(key="fictional-notice-gift"):
        with django_capture_on_commit_callbacks(execute=True):
            return transfer(a, key, bm.number, "3.00")

    return SimpleNamespace(
        a=a,
        b=b,
        am=am,
        bm=bm,
        bindings=bindings,
        provider=provider,
        dispatched=dispatched,
        dispatch=original_dispatch,
        gift=gift,
    )


def test_only_new_operations_queue_both_gift_sides_once_after_commit(rig):
    assert Ledger.objects.count() == 1 and not MovementNotice.objects.exists()
    rig.gift()
    assert MovementNotice.objects.count() == 2 and len(rig.dispatched) == 2
    rig.gift()  # The same confirmed financial request cannot make another notice.
    assert MovementNotice.objects.count() == 2 and len(rig.dispatched) == 2
    for row in MovementNotice.objects.order_by("ledger__sequence"):
        notices.deliver_movement_notice(row.pk)
        notices.deliver_movement_notice(row.pk)
        row.refresh_from_db()
        assert row.status == "accepted" and row.attempts == 1
    assert rig.provider.send_image.call_count == 2 and not rig.provider.send_text.called
    outgoing, incoming = [c.kwargs for c in rig.provider.send_image.call_args_list]
    assert outgoing["recipient"] == rig.bindings[0].phone
    assert incoming["recipient"] == rig.bindings[1].phone
    assert rig.bm.number in outgoing["caption"] and rig.am.number in incoming["caption"]
    assert "أهديت إلى" in outgoing["caption"] and "هدية من" in incoming["caption"]
    assert "١٩.٥٠" in outgoing["caption"] and "٣.٠٠" in incoming["caption"]
    assert balance(rig.am) == Decimal("19.50") and balance(rig.bm) == Decimal("3.00")
    assert not rig.provider.send_text.called


def test_recovery_never_scans_or_notifies_existing_ledger_rows(rig):
    assert notices.recover_notices() == {"status": "completed", "queued": 0}
    assert not MovementNotice.objects.exists() and not rig.dispatched
    assert not rig.provider.send_image.called


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_stay_reward_notice_has_rate_points_balance_logo_and_saved_single_language(rig, language):
    profile = profile_for(rig.a)
    profile.preferred_language = language
    profile.save()
    reconcile(booking(rig.a, "957.00").pk)
    notice = MovementNotice.objects.get()
    assert notice.language == language
    profile.preferred_language = "ar" if language != "ar" else "fr"
    profile.save()
    with override("fr" if language != "fr" else "en"):
        notices.deliver_movement_notice(notice.pk)
    packet = rig.provider.send_image.call_args.kwargs
    expected = {"ar": "مكافأة إقامة", "en": "Stay reward", "fr": "Points de séjour"}
    assert expected[language] in packet["caption"]
    assert all(expected[other] not in packet["caption"] for other in expected if other != language)
    assert ("٣٪" if language == "ar" else "3%") in packet["caption"]
    assert packet["content"] == notices.phone_logo()
    assert packet["content"].startswith(b"\x89PNG")
    assert len(packet["caption"].encode("utf-16-le")) // 2 <= 1024
    assert packet["reference_id"].startswith("lsa-rewards-")
    assert "LSA-AUTH" not in packet["caption"]
    assert "/my-bookings/rewards/" in packet["caption"]
    assert rig.b.email not in packet["caption"] and rig.bindings[1].phone not in packet["caption"]


def test_rollback_removes_postings_and_notices_and_never_dispatches(
    rig, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError), transaction.atomic():
            transfer(rig.a, "fictional-rollback", rig.bm.number, "3.00")
            raise RuntimeError("synthetic rollback")
    assert Ledger.objects.count() == 1
    assert not MovementNotice.objects.exists() and not rig.dispatched


def test_missing_proof_is_recorded_without_sending_or_preventing_points(rig):
    rig.bindings[1].delete()
    rig.gift()
    row = MovementNotice.objects.get(ledger__member=rig.bm)
    assert row.status == "skipped" and row.error_code == "verified_mobile_required"
    notices.deliver_movement_notice(row.pk)
    assert not rig.provider.send_image.called and balance(rig.bm) == Decimal("3.00")


@pytest.mark.parametrize("change", ["version", "phone", "deleted", "inactive"])
def test_recipient_change_fails_closed_without_disclosing_to_a_new_number(rig, change):
    rig.gift()
    row = MovementNotice.objects.get(ledger__member=rig.bm)
    if change == "deleted":
        rig.bindings[1].delete()
    elif change in {"version", "phone"}:
        import uuid

        binding = rig.bindings[1]
        if change == "version":
            binding.version = uuid.uuid4()
        else:
            binding.phone = "+15550003333"
        binding.save()
    elif change == "inactive":
        rig.b.is_active = False
        rig.b.save()
    notices.deliver_movement_notice(row.pk)
    row.refresh_from_db()
    assert row.status == "skipped" and not rig.provider.send_image.called


def test_verified_mobile_notice_does_not_require_access_to_old_email(rig):
    rig.gift()
    row = MovementNotice.objects.get(ledger__member=rig.bm)
    profile = profile_for(rig.b)
    profile.email_verified_at = None
    profile.save(update_fields=["email_verified_at"])
    notices.deliver_movement_notice(row.pk)
    row.refresh_from_db()
    assert row.status == "accepted" and rig.provider.send_image.called


def test_unknown_provider_outcome_is_not_retried_or_logged_and_keeps_balances(rig, caplog):
    rig.gift()
    row = MovementNotice.objects.get(ledger__member=rig.am)
    rig.provider.send_image.side_effect = RuntimeError("synthetic-private-provider-body")
    notices.deliver_movement_notice(row.pk)
    notices.deliver_movement_notice(row.pk)
    row.refresh_from_db()
    assert row.status == "uncertain" and rig.provider.send_image.call_count == 1
    assert not rig.provider.send_text.called
    assert "synthetic-private-provider-body" not in caplog.text
    assert balance(rig.am) == Decimal("19.50") and balance(rig.bm) == Decimal("3.00")


def test_disconnected_provider_can_retry_only_before_submission(rig, notice_clock):
    rig.gift()
    row = MovementNotice.objects.get(ledger__member=rig.am)
    rig.provider.http.get.side_effect = RuntimeError("synthetic-connect-error")
    notices.deliver_movement_notice(row.pk)
    row.refresh_from_db()
    assert row.status == "queued" and not rig.provider.send_image.called
    notices.deliver_movement_notice(row.pk)
    assert rig.provider.http.get.call_count == 1
    notice_clock[0] += timedelta(minutes=3)
    rig.provider.http.get.side_effect = None
    notices.deliver_movement_notice(row.pk)
    row.refresh_from_db()
    assert row.status == "accepted" and rig.provider.send_image.call_count == 1


def test_delayed_queue_is_durable_but_stale_worker_is_not_replayed(rig, notice_clock):
    rig.gift()
    first = MovementNotice.objects.get(ledger__member=rig.am)
    second = MovementNotice.objects.get(ledger__member=rig.bm)
    MovementNotice.objects.filter(pk=first.pk).update(status="sending", started_at=timezone.now())
    notice_clock[0] += timedelta(hours=25)
    notices.recover_notices()
    first.refresh_from_db()
    assert first.status == "uncertain"
    notices.deliver_movement_notice(second.pk)
    second.refresh_from_db()
    assert second.status == "accepted" and rig.provider.send_image.call_count == 1


def test_unavailable_provider_and_feature_off_leave_financial_results_alone(rig, settings):
    rig.gift()
    settings.ULTRAMSG_ENABLED = False
    row = MovementNotice.objects.get(ledger__member=rig.am)
    notices.deliver_movement_notice(row.pk)
    row.refresh_from_db()
    assert row.status == "queued" and row.attempts == 0
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = False
    assert notices.recover_notices()["status"] == "disabled"
    assert not rig.provider.send_image.called and balance(rig.am) == Decimal("19.50")


def test_queue_failure_isolated_from_the_financial_transaction(rig, monkeypatch):
    monkeypatch.setattr(notices, "queue_notice", Mock(side_effect=DatabaseError("synthetic-only")))
    rig.gift()
    assert balance(rig.bm) == Decimal("3.00") and Ledger.objects.count() == 3


def test_member_notifications_stay_in_transaction_order(rig):
    rig.gift()
    rig.gift("fictional-second-transfer")
    rows = list(MovementNotice.objects.filter(ledger__member=rig.am).order_by("ledger__sequence"))
    notices.deliver_movement_notice(rows[1].pk)
    assert not rig.provider.send_image.called
    notices.deliver_movement_notice(rows[0].pk)
    notices.deliver_movement_notice(rows[1].pk)
    assert rig.provider.send_image.call_count == 2


def test_membership_close_still_notifies_its_owner(rig, notice_clock):
    notice_clock[0] += timedelta(days=3)
    withdraw(rig.a, "fictional-close-notice")
    row = MovementNotice.objects.get()
    notices.deliver_movement_notice(row.pk)
    assert "إقفال العضوية" in rig.provider.send_image.call_args.kwargs["caption"]


def test_dispatch_failure_is_durable_without_financial_rollback(rig, monkeypatch):
    monkeypatch.setattr(
        notices.deliver_movement_notice, "delay", Mock(side_effect=RuntimeError("broker-offline"))
    )
    rig.gift()
    for key in rig.dispatched:
        rig.dispatch(key)
    assert MovementNotice.objects.filter(status="queued").count() == 2
    assert balance(rig.bm) == Decimal("3.00")


def test_owner_can_inspect_notice_state_without_sending_again(rig):
    rig.gift()
    html = login(owner()).get("/admin/loyalty/").content.decode()
    assert "إشعار واتساب" in html and "بانتظار الإرسال" in html
    assert rig.bindings[0].phone not in html and rig.bindings[1].phone not in html
    assert not rig.provider.send_image.called


def test_notice_scheduler_does_not_enable_email_or_invitations(settings):
    from django.conf import settings as django_settings

    values = {
        name: getattr(django_settings, name) for name in dir(django_settings) if name.isupper()
    }
    values.update(LOYALTY_WHATSAPP_NOTICES_ENABLED=True, LOYALTY_MESSAGES_ENABLED=False)
    schedule = build_beat_schedule(values)
    assert schedule["loyalty-movement-whatsapp"]["schedule"] == 60
    assert "loyalty-guest-invitations" not in schedule


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize(
    "kind,event",
    [
        ("loyalty", "earn"),
        ("direct_referral", "earn"),
        ("indirect_referral", "earn"),
        ("addition", "earn"),
        ("transfer", "transfer"),
        ("transfer", "redeem"),
        ("addition", "restore"),
        ("addition", "adjust"),
        ("transfer", "withdraw"),
    ],
)
def test_every_posted_operation_type_has_a_short_private_localized_notice(
    rig, language, kind, event
):
    row = Ledger.objects.first()
    row.operation_type, row.event_type = kind, event
    row.reservation_reference = "PRIVATE-BOOKING-NOT-FOR-MESSAGES"
    row.property_name = "PRIVATE-APARTMENT-NOT-FOR-MESSAGES"
    caption = notices.movement_caption(row, language)
    assert row.ui_kind in caption and row.operation_number in caption
    assert "PRIVATE-" not in caption and rig.a.email not in caption
    assert len(caption.encode("utf-16-le")) // 2 <= 1024


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Concurrent claims require PostgreSQL"
)
@pytest.mark.django_db(transaction=True)
def test_parallel_notice_claim_submits_only_once_on_postgres(rig):
    if connection.vendor != "postgresql":
        pytest.skip("Concurrent delivery claims require the isolated PostgreSQL job")
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections

    rig.gift()
    key = MovementNotice.objects.get(ledger__member=rig.am).pk

    def deliver():
        close_old_connections()
        try:
            notices.deliver_movement_notice(key)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: deliver(), range(2)))
    assert rig.provider.send_image.call_count == 1
