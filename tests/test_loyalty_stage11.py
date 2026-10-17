"""Cross-stage acceptance tests. Offline and fictional; not a live launch proof."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core.models import FAQItem, SitePage
from apps.loyalty.backup import LocalMirror, capture, restore_local, validate
from apps.loyalty.checkout import cash_due, confirm_points_booking
from apps.loyalty.models import Ledger, Member, Referral
from apps.loyalty.services import (
    available,
    balance,
    confirm_arrival,
    hold,
    issue_invite,
    reconcile,
    redeem,
    transfer,
)
from apps.loyalty.site_adapter import schedule_reconciliation
from apps.loyalty.stay import record_departure
from apps.payments.hyperpay.exceptions import HyperPayRefundError
from apps.payments.hyperpay.refunds import HyperPayRefundService
from apps.reservations.models import RefundObligation, Reservation
from apps.reservations.services.refunds import RefundComputation, record_obligation
from tests.test_hyperpay_refunds_phase94 import REFUND_SETTINGS, RefundStub
from tests.test_loyalty_stage8 import booking, completed_change, join, payment, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clock(settings, monkeypatch):
    settings.LOYALTY_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    times = [datetime(2026, 10, 11, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: times[0])
    return times


@pytest.fixture
def migrated_recovery_db(tmp_path, django_db_blocker):
    """Replay every real migration on a separate, newly-created test database."""
    alias = "loyalty_full_migration_recovery"
    connections.databases[alias] = {
        **connections["default"].settings_dict,
        "NAME": str(tmp_path / "fresh-recovery.sqlite3"),
    }
    try:
        with django_db_blocker.unblock():
            with CaptureQueriesContext(connections["default"]) as source_queries:
                executor = MigrationExecutor(connections[alias])
                targets = executor.loader.graph.leaf_nodes()
                executor.migrate(targets)
                assert MigrationExecutor(connections[alias]).migration_plan(targets) == []
            # Secondary migration must not read, seed or normalize the source.
            assert len(source_queries) == 0
            yield alias
    finally:
        if hasattr(connections._connections, alias):
            connections[alias].close()
            del connections[alias]
        connections.databases.pop(alias, None)


def audit_ledger():
    program = Decimal(0)
    accounts = {}
    groups = {}
    for expected_sequence, row in enumerate(Ledger.objects.all(), start=1):
        assert row.sequence == expected_sequence
        accounts[row.member_id] = accounts.get(row.member_id, Decimal(0)) + row.credit - row.debit
        program += row.credit - row.debit
        assert row.member_balance_after == accounts[row.member_id]
        assert row.program_balance_after == program
        assert row.membership_number == row.member.number
        assert row.tree_number == row.member.referral.tree_number
        assert row.executed_by == "system"
        if row.event_type == "transfer":
            groups.setdefault(row.operation_group, []).append(row)
    for pair in groups.values():
        assert len(pair) == 2
        assert pair[0].debit == pair[1].credit
        assert pair[0].counterpart_id == pair[1].member_id
        assert pair[1].counterpart_id == pair[0].member_id
    assert sum(accounts.values(), Decimal(0)) == program
    return program


def test_full_migration_restore_does_not_touch_source(migrated_recovery_db):
    a, b = user("recover-a"), user("recover-b")
    am, bm = join(a), join(b)
    reconcile(booking(a).pk)
    transfer(a, "TEST-RECOVERY-TRANSFER", bm.number, "10.00")
    original = capture(source_id="stage-eleven-fictional")
    original_faqs = list(FAQItem.objects.values())
    original_pages = list(SitePage.objects.values())
    recovered = restore_local(original, using=migrated_recovery_db)
    assert recovered == validate(original)
    assert recovered["program_balance"] == "22.50"
    assert Ledger.objects.using(migrated_recovery_db).count() == 3
    assert FAQItem.objects.using(migrated_recovery_db).exists()
    assert list(FAQItem.objects.values()) == original_faqs
    assert list(SitePage.objects.values()) == original_pages
    assert capture(source_id=original["source_id"]) == original
    assert balance(am) == Decimal("12.50") and balance(bm) == Decimal("10.00")


@pytest.mark.parametrize("first_event", ["arrival", "payment", "joining"])
def test_event_order_awards_once_only_after_all_three_facts(
    first_event,
    clock,
    django_capture_on_commit_callbacks,
):
    u = user("ordering")
    operator = user("reception")
    operator.is_staff = operator.is_superuser = True
    operator.save()
    r = booking(u, paid=False, arrived=False)
    events = [first_event] + [
        name for name in ["joining", "arrival", "payment"] if name != first_event
    ]
    for position, event in enumerate(events):
        if event == "joining":
            join(u)
        elif event == "arrival":
            confirm_arrival(
                operator,
                r.pk,
                arrived_at=clock[0] - timedelta(minutes=30),
                reference="TEST-ARRIVED-RECEPTION",
            )
        else:
            payment(r, "750.00")
        with django_capture_on_commit_callbacks(execute=True):
            schedule_reconciliation(r.pk)
            schedule_reconciliation(r.pk)
        assert Ledger.objects.count() == (1 if position == 2 else 0)
    assert balance(Member.objects.get(customer=u)) == Decimal("22.50")
    assert audit_ledger() == Decimal("22.50")


@pytest.mark.parametrize("depth", [1, 3, 10, 31])
def test_site_native_long_referral_chain_honors_total_cap(depth, clock):
    parent, token = None, None
    members = []
    for level in range(depth):
        u = user(f"chain-{level}")
        inviter = {"invitation_token": token, "confirmed_inviter": parent.pk} if parent else {}
        m = join(u, **inviter)
        members.append(m)
        r = booking(u, "750.00")
        reconcile(r.pk)
        rows = list(Ledger.objects.filter(reservation=r, event_type="earn"))
        assert sum((row.credit for row in rows), Decimal(0)) <= Decimal("112.50")
        indirect = [row.credit for row in rows if row.component == "indirect_referral"]
        assert all(value <= Decimal("7.50") for value in indirect)
        assert sum(indirect, Decimal(0)) <= Decimal("52.50")
        if level > 8:
            assert max(indirect) - min(indirect) <= Decimal("0.01")
            assert sum(indirect, Decimal(0)) == Decimal("52.50")
        clock[0] += timedelta(days=3)
        parent, token = m, issue_invite(u, r.pk)
    assert Referral.objects.last().depth == depth - 1
    assert len(Referral.objects.last().tree_number.split("-")) == depth
    assert audit_ledger() == sum((balance(m) for m in members), Decimal(0))
    validate(capture(source_id="stage-eleven-chain"))


def test_transfer_second_write_failure_rolls_back_both_sides_and_receipt(monkeypatch):
    from apps.loyalty import services

    a, b = user("atomic-a"), user("atomic-b")
    am, bm = join(a), join(b)
    reconcile(booking(a).pk)
    before = capture(source_id="stage-eleven-atomic")
    real_post = services._post

    def interrupted(member, group, branch, **fields):
        if fields.get("component") == "transfer-in":
            raise RuntimeError("fictional-write-failure")
        return real_post(member, group, branch, **fields)

    with monkeypatch.context() as scope:
        scope.setattr(services, "_post", interrupted)
        with pytest.raises(RuntimeError):
            transfer(a, "TEST-ATOMIC-TRANSFER", bm.number, "10.00")
    assert capture(source_id=before["source_id"]) == before
    successful = transfer(a, "TEST-ATOMIC-TRANSFER", bm.number, "10.00")
    assert transfer(a, "TEST-ATOMIC-TRANSFER", bm.number, "10.00") == successful
    assert balance(am) == Decimal("12.50") and balance(bm) == Decimal("10.00")
    assert audit_ledger() == Decimal("22.50")


def test_reservation_holds_prevent_a_second_booking_or_transfer_spending_same_points():
    a, b = user("hold-a"), user("hold-b")
    am, bm = join(a), join(b)
    reconcile(booking(a).pk)
    first, second = [booking(a, "100.00", paid=False, arrived=False) for _ in range(2)]
    for r in [first, second]:
        r.normalized_status = Reservation.Status.AWAITING_PAYMENT
        r.save(update_fields=["normalized_status"])
    hold(a, "TEST-HOLD-FIRST", first.pk, "20.00")
    with pytest.raises(ValidationError):
        hold(a, "TEST-HOLD-SECOND", second.pk, "20.00")
    with pytest.raises(ValidationError):
        transfer(a, "TEST-HOLD-TRANSFER", bm.number, "20.00")
    assert available(am) == Decimal("2.50") and balance(bm) == 0
    assert audit_ledger() == Decimal("22.50")


def test_signed_correction_multiple_future_stays_and_backup_restart(tmp_path):
    a, b = user("offset-a"), user("offset-b")
    am, bm = join(a), join(b)
    first = booking(a)
    reconcile(first.pk)
    transfer(a, "TEST-OFFSET-ALL", bm.number, "22.50")
    completed_change(first, "250.00")
    reconcile(first.pk)
    am.refresh_from_db()
    assert not am.deferred_corrections and balance(am) == Decimal("-15.00")
    for n in range(1, 4):
        reconcile(booking(a, "100.00", reference=f"TEST-NEXT-{n}").pk)
        assert balance(am) == Decimal("-15.00") + Decimal("3.00") * n
    am.refresh_from_db()
    assert not am.deferred_corrections and balance(am) == Decimal("-6.00")
    final = booking(a, "250.00")
    reconcile(final.pk)
    assert balance(am) == Decimal("1.50")
    assert balance(bm) == Decimal("22.50")
    am.refresh_from_db()
    assert not am.deferred_corrections
    assert reconcile(final.pk)["movements"] == 0
    assert audit_ledger() == Decimal("24.00")
    data = capture(source_id="stage-eleven-restart")
    path = tmp_path / "daily-fixed.sqlite3"
    LocalMirror(path).update(data)
    # A new instance/process can read durable tables, not Python memory.
    reopened = LocalMirror(path)
    assert reopened.read() == data
    assert reopened.status()["status"] == "succeeded"


def test_unpaid_completed_booking_must_not_issue_invitation(clock):
    u = user("unpaid-invite")
    m = join(u)
    r = booking(u, paid=False)
    clock[0] += timedelta(days=3)
    with pytest.raises(ValidationError):
        issue_invite(u, r.pk)
    m.refresh_from_db()
    assert not m.invitation_token and m.first_completed_at is None
    payment(r, "750.00")
    assert issue_invite(u, r.pk)


def test_first_stay_funded_by_transferred_points_can_unlock_invitation(clock):
    from apps.payments.models import PaymentAttempt

    donor, guest = user("gift-donor"), user("gift-guest")
    join(donor)
    gm = join(guest)
    reconcile(booking(donor).pk)
    transfer(donor, "TEST-FIRST-STAY-GIFT", gm.number, "20.00")
    r = booking(guest, "20.00", paid=False, arrived=False)
    r.booking_intent.payment_amount_sar = Decimal("20.00")
    r.booking_intent.save(update_fields=["payment_amount_sar"])
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save(update_fields=["normalized_status"])
    hold(guest, "TEST-GIFT-HOLD", r.pk, "20.00")

    def confirmed_inventory(reservation):
        reservation.normalized_status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=["normalized_status"])
        return SimpleNamespace(code="confirmed")

    confirm_points_booking(
        guest,
        r.pk,
        booking_service=SimpleNamespace(
            create_points_reservation=confirmed_inventory,
        ),
    )
    operator = user("gift-reception")
    operator.is_staff = operator.is_superuser = True
    operator.save()
    confirm_arrival(
        operator,
        r.pk,
        arrived_at=clock[0] - timedelta(minutes=30),
        reference="TEST-GIFT-ARRIVAL",
    )
    assert not PaymentAttempt.objects.filter(booking_intent=r.booking_intent).exists()
    assert not Ledger.objects.filter(reservation=r, event_type="earn").exists()
    clock[0] += timedelta(days=3)
    assert issue_invite(guest, r.pk)
    gm.refresh_from_db()
    assert gm.first_completed_reservation_id == r.pk and balance(gm) == 0


def test_completed_paid_booking_outside_launch_scope_cannot_unlock_invitation(clock):
    u = user("scope-invite")
    join(u)
    r = booking(u)
    r.source_type = Reservation.SourceType.EXTERNAL_CHANNEL
    r.save(update_fields=["source_type"])
    clock[0] += timedelta(days=3)
    with pytest.raises(ValidationError):
        issue_invite(u, r.pk)


def test_isolated_migration_does_not_create_users_in_source(migrated_recovery_db):
    assert not get_user_model().objects.exists()
    assert not get_user_model().objects.using(migrated_recovery_db).exists()


def test_reverse_empty_target_migrations_keeps_source_unchanged(migrated_recovery_db):
    # Do not claim an arbitrary populated modern database can be downgraded
    # across all historical constraints. This checks alias isolation only.
    u = user("reverse-source")
    original_faqs = list(FAQItem.objects.values())
    original_pages = list(SitePage.objects.values())
    with CaptureQueriesContext(connections["default"]) as source_queries:
        executor = MigrationExecutor(connections[migrated_recovery_db])
        executor.migrate([(app, None) for app in executor.loader.migrated_apps])
    assert len(source_queries) == 0
    assert get_user_model().objects.get(pk=u.pk).email == u.email
    assert list(FAQItem.objects.values()) == original_faqs
    assert list(SitePage.objects.values()) == original_pages


@pytest.mark.parametrize(
    "departure_hour,card_refund,points_refund,final_balance",
    [(None, "730.00", "20.00", "22.50"), (19, "486.66", "13.34", "23.14")],
)
@override_settings(
    **{
        **REFUND_SETTINGS,
        # Exercise the production two-decimal formatter with a local stub.
        # The offline runner forbids all network connections unconditionally.
        "HYPERPAY_ENVIRONMENT": "production",
        "HYPERPAY_BASE_URL": "https://eu-prod.oppwa.com/",
        "HYPERPAY_REFUNDS_PRODUCTION_ENABLED": True,
    }
)
def test_mixed_refund_confirmed_callback_reconciles_cash_points_and_rewards(
    departure_hour,
    card_refund,
    points_refund,
    final_balance,
    clock,
    django_capture_on_commit_callbacks,
    monkeypatch,
):
    # Real site service/records, but the provider and notices are local fakes.
    # A trusted synthetic nightly snapshot prices departure; quote capture is
    # still a release gap, not something this test pretends to implement.
    import apps.payments.hyperpay.refunds as refunds

    monkeypatch.setattr(refunds, "_queue_guest_refund_notice", lambda _id: None)
    u = user("mixed-refund")
    member = join(u)
    reconcile(booking(u).pk)
    r = booking(u, paid=False, arrived=False)
    r.booking_intent.payment_amount_sar = Decimal("750.00")
    r.booking_intent.save(update_fields=["payment_amount_sar"])
    r.normalized_status = Reservation.Status.AWAITING_PAYMENT
    r.save(update_fields=["normalized_status"])
    hold(u, "TEST-MIXED-HOLD", r.pk, "20.00")
    assert cash_due(r.booking_intent) == Decimal("730.00")
    r.normalized_status = Reservation.Status.CONFIRMED
    r.save(update_fields=["normalized_status"])
    redeem(u, "TEST-MIXED-REDEEM", r.pk)
    payment(r, "730.00")
    if departure_hour:
        operator = user("mixed-reception")
        operator.is_staff = operator.is_superuser = True
        operator.save()
        confirm_arrival(
            operator,
            r.pk,
            arrived_at=clock[0] - timedelta(minutes=30),
            reference="TEST-MIXED-ARRIVAL",
        )
        r.refresh_from_db()
        r.loyalty_state["priced_nights"] = {
            "currency": "SAR",
            "reference": "TEST-TRUSTED-CONTRACT",
            "nights": [
                {"date": r.check_in.isoformat(), "value": "250.00"},
                {"date": (r.check_in + timedelta(days=1)).isoformat(), "value": "500.00"},
            ],
        }
        r.save(update_fields=["loyalty_state"])
        clock[0] = (clock[0] + timedelta(days=1)).replace(hour=departure_hour - 3)
        plan = record_departure(
            operator,
            r.pk,
            vacated_at=clock[0],
            reference="TEST-MIXED-EXIT",
        )
        assert Decimal(plan["cash_refund"]) == Decimal(card_refund)
        assert Decimal(plan["points_refund"]) == Decimal(points_refund)
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CANCELLED
    r.save(update_fields=["normalized_status"])
    obligation = record_obligation(
        r,
        reason=RefundObligation.Reason.CANCELLATION,
        computation=RefundComputation(Decimal(card_refund), "SAR", {"source": "fictional-plan"}),
    )
    gateway = RefundStub({"id": "TEST-REFUND-RECEIPT", "result": {"code": "000.100.110"}})
    service = HyperPayRefundService(client=gateway)
    with django_capture_on_commit_callbacks(execute=True):
        outcome = service.submit(obligation, operator=object())
    assert outcome.audit_action == "refund.hyperpay_completed"
    assert gateway.calls[0][1]["amount"] == card_refund
    assert balance(member) == Decimal(final_balance)
    r.refresh_from_db()
    assert Decimal(r.loyalty_state["redemption"]["restored"]) == Decimal(points_refund)
    count = Ledger.objects.count()
    with pytest.raises(HyperPayRefundError):
        service.submit(obligation, operator=object())
    with django_capture_on_commit_callbacks(execute=True):
        schedule_reconciliation(r.pk)
    assert len(gateway.calls) == 1 and Ledger.objects.count() == count
    assert audit_ledger() == Decimal(final_balance)
