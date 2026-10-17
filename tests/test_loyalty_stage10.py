"""Offline backup/update/restore rehearsal with fictional site-native records."""

import copy
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, connections
from django.utils import timezone

from apps.loyalty.backup import (
    BackupError,
    LocalMirror,
    _models,
    capture,
    compare,
    restore_local,
    seal,
    validate,
)
from apps.loyalty.models import Ledger, Member, Referral
from apps.loyalty.services import balance, hold, issue_invite, reconcile, transfer
from apps.reservations.models import Reservation
from tests.test_loyalty_stage8 import booking, completed_inviter, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def local_program(settings, monkeypatch):
    settings.LOYALTY_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    return clock


@pytest.fixture
def sequence(local_program):
    a, b, c, d = [user(n) for n in range(1, 5)]
    am, token = completed_inviter(a, local_program)
    bm = join(b, invitation_token=token, confirmed_inviter=am.pk)
    br = booking(b, "950.00")
    reconcile(br.pk)
    local_program[0] += timedelta(days=3)
    bt = issue_invite(b, br.pk)
    cm = join(c, invitation_token=bt, confirmed_inviter=bm.pk)
    dm = join(d, invitation_token=bt, confirmed_inviter=bm.pk)
    reconcile(booking(c, "850.00").pk)
    reconcile(booking(d, "650.00").pk)
    transfer(a, "TEST-TRANSFER-20", dm.number, "20.00")
    return a, b, c, d, (am, bm, cm, dm)


def snapshot():
    return capture(source_id="fictional-local-stage-ten")


def row(snapshot, model, n=0):
    return [r for r in snapshot["records"] if r["model"] == model][n]


def test_master_example_snapshot_preserves_tree_balances_and_references(sequence):
    a, b, c, d, members = sequence
    data = snapshot()
    result = validate(data)
    assert result["counts"]["loyalty.ledger"] == 11
    assert result["program_balance"] == "233.50"
    assert list(result["member_balances"].values()) == ["65.00", "103.50", "25.50", "39.50"]
    assert row(data, "loyalty.referral", 3)["fields"]["tree_number"] == "0001-0001-0002"
    assert row(data, "loyalty.member")["fields"]["consent_history"]
    assert row(data, "loyalty.member")["fields"]["command_receipts"]["TEST-TRANSFER-20"]
    assert result["counts"]["payments.paymentattempt"] == 4
    assert result["counts"]["accounts.customerprofile"] == 4
    assert row(data, "reservations.reservation")["fields"]["loyalty_state"]["arrival"]


def test_snapshot_preserves_existing_booking_payment_requests_and_delivery_evidence(sequence):
    from apps.payments.models import (
        PaymentAttempt,
        PaymentRequestDelivery,
        ReservationPaymentRequest,
    )

    stay = Reservation.objects.first()
    request = ReservationPaymentRequest.objects.create(
        reservation=stay,
        amount_sar=stay.total_price,
        source_amount=stay.total_price,
        source_currency=stay.currency,
        fingerprint="fictional-snapshot-test",
        expires_at=timezone.now() + timedelta(days=1),
    )
    delivery = PaymentRequestDelivery.objects.create(
        request=request, kind="link", status="unknown", error_code="delivery_outcome_uncertain"
    )
    PaymentAttempt.objects.filter(booking_intent=stay.booking_intent).update(
        payment_request=request
    )
    data = snapshot()
    validate(data)
    assert row(data, "payments.reservationpaymentrequest")["pk"] == str(request.pk)
    assert row(data, "payments.paymentrequestdelivery")["pk"] == delivery.pk
    assert row(data, "payments.paymentrequestdelivery")["fields"]["status"] == "unknown"


@pytest.mark.parametrize("legacy_deadline", [False, True])
def test_payment_link_nullable_deadline_backup_and_restore(
    sequence, recovery_db, tmp_path, legacy_deadline
):
    from apps.loyalty.backup import _schema_hash
    from apps.payments.models import ReservationPaymentRequest

    stay = Reservation.objects.first()
    expiry = timezone.now() - timedelta(days=1) if legacy_deadline else None
    request = ReservationPaymentRequest.objects.create(
        reservation=stay,
        amount_sar=stay.total_price,
        source_amount=stay.total_price,
        source_currency=stay.currency,
        fingerprint="fictional-nullable-expiry",
        expires_at=expiry,
    )
    data = snapshot()
    if legacy_deadline:
        data["schema"] = _schema_hash(legacy_link_expiry=True)
        data = seal(data)
    original = copy.deepcopy(data)
    validate(data)
    mirror = LocalMirror(tmp_path / "nullable-expiry.sqlite3")
    mirror.update(data)
    assert mirror.read() == original
    restore_local(data, using=recovery_db)
    restored = ReservationPaymentRequest.objects.using(recovery_db).get(pk=request.pk)
    assert restored.expires_at == expiry and restored.status == "open"
    assert data == original


def test_known_pre_payment_link_backup_survives_schema_transition(sequence, tmp_path):
    from apps.loyalty.backup import _schema_hash

    incoming = snapshot()
    previous = copy.deepcopy(incoming)
    previous["schema"] = _schema_hash(payment_links=False)
    for record in previous["records"]:
        if record["model"] == "payments.paymentattempt":
            record["fields"].pop("payment_request")
    previous = seal(previous)
    original = copy.deepcopy(previous)
    validate(previous)
    compare(previous, incoming)
    mirror = LocalMirror(tmp_path / "previous-schema.sqlite3")
    mirror.update(previous)
    assert mirror.read() == previous
    mirror.update(incoming)
    assert mirror.read() == incoming
    assert previous == original
    unsafe = copy.deepcopy(previous)
    unsafe["schema"] = "unreviewed-older-schema"
    with pytest.raises(BackupError) as error:
        validate(seal(unsafe))
    assert error.value.code == "incompatible_snapshot_schema"


def test_legacy_schema_cannot_hide_a_missing_payment_reference(sequence):
    from apps.loyalty.backup import _schema_hash

    data = snapshot()
    data["schema"] = _schema_hash(payment_links=False)
    # Current fields cannot masquerade as an old snapshot to bypass closure checks.
    with pytest.raises(BackupError) as error:
        validate(seal(data))
    assert error.value.code == "unreviewed_record_fields"


def test_zero_points_member_and_pending_booking_hold_are_not_lost(sequence):
    a, _, _, _, members = sequence
    join(user("no-points"))
    pending = booking(a, "100.00", paid=False, arrived=False)
    pending.normalized_status = Reservation.Status.AWAITING_PAYMENT
    pending.save(update_fields=["normalized_status"])
    hold(a, "TEST-HOLD-10", pending.pk, "10.00")
    data = snapshot()
    assert validate(data)["counts"]["loyalty.member"] == 5
    captured = next(
        r
        for r in data["records"]
        if r["model"] == "reservations.reservation" and str(r["pk"]) == str(pending.pk)
    )
    assert Decimal(captured["fields"]["loyalty_state"]["hold"]["value"]) == Decimal("10.00")
    assert validate(data)["member_balances"][str(Member.objects.last().pk)] == "0.00"


def test_registration_events_do_not_export_unrelated_bookings_or_auth_sessions(sequence):
    stranger = user("unrelated")
    unrelated_booking = booking(stranger)
    data = snapshot()
    # Registration/proof events are now in scope even before loyalty enrollment.
    assert validate(data)["counts"]["auth.user"] == 5
    assert not any(r["model"].startswith(("sessions.", "socialaccount.")) for r in data["records"])
    assert "groups" not in row(data, "auth.user")["fields"]
    assert any(r["fields"].get("email") == stranger.email for r in data["records"])
    assert not any(
        r["model"] == "reservations.reservation" and str(r["pk"]) == str(unrelated_booking.pk)
        for r in data["records"]
    )


def test_fixed_tables_are_updated_not_duplicated(sequence, tmp_path):
    mirror = LocalMirror(tmp_path / "latest.sqlite3")
    data = snapshot()
    result = mirror.update(data)
    assert validate(mirror.read()) == result
    assert mirror.read() == data
    assert mirror.update(data) == result
    a, _, _, _, members = sequence
    transfer(a, "TEST-SECOND-TRANSFER", members[-1].number, "1.00")
    fresh = snapshot()
    mirror.update(fresh)
    assert mirror.read() == fresh
    with sqlite3.connect(mirror.path) as db:
        names = {x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names == {"members", "referrals", "ledger", "context", "manifest", "status"}
        assert db.execute("SELECT COUNT(*) FROM manifest").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 13
        assert db.execute("SELECT COUNT(*) FROM status").fetchone()[0] == 1
    assert mirror.status()["status"] == "succeeded"


@pytest.mark.parametrize(
    "problem,code",
    [
        ("digest", "snapshot_integrity_failed"),
        ("missing-payment", "missing_related_record"),
        ("payment-evidence", "missing_payment_evidence"),
        ("balance", "ledger_balance_mismatch"),
        ("fraction", "invalid_points_precision"),
        ("tree", "invalid_membership_tree"),
        ("sequence", "invalid_ledger_sequence"),
        ("duplicate", "duplicate_record"),
        ("schema", "incompatible_snapshot_schema"),
        ("fields", "unreviewed_record_fields"),
        ("time", "invalid_snapshot_time"),
    ],
)
def test_invalid_snapshot_rejected_before_touching_good_copy(sequence, tmp_path, problem, code):
    mirror = LocalMirror(tmp_path / "latest.sqlite3")
    good = snapshot()
    mirror.update(good)
    bad = copy.deepcopy(good)
    if problem == "digest":
        row(bad, "loyalty.member")["fields"]["number"] = "FORGED"
    elif problem == "missing-payment":
        # Removing a customer leaves multiple explicit protected foreign keys missing.
        bad["records"].remove(row(bad, "auth.user"))
    elif problem == "payment-evidence":
        bad["records"].remove(row(bad, "payments.paymentattempt"))
    elif problem == "balance":
        row(bad, "loyalty.ledger")["fields"]["program_balance_after"] = "999.00"
    elif problem == "fraction":
        row(bad, "loyalty.ledger")["fields"]["credit"] = "22.501"
    elif problem == "tree":
        row(bad, "loyalty.referral")["fields"]["depth"] = 1
    elif problem == "sequence":
        row(bad, "loyalty.ledger")["fields"]["sequence"] = 50
    elif problem == "duplicate":
        bad["records"].append(row(bad, "loyalty.member"))
    elif problem == "schema":
        bad["schema"] = "unknown"
    elif problem == "fields":
        del row(bad, "loyalty.member")["fields"]["command_receipts"]
    elif problem == "time":
        bad["captured_at"] = "2026-10-10"
    if problem != "digest":
        bad = seal(bad)
    with pytest.raises(BackupError) as error:
        mirror.update(bad)
    assert error.value.code == code
    assert mirror.read() == good
    assert mirror.status()["last_success"]
    assert mirror.status()["status"] == "failed"
    assert mirror.status()["error_code"] == code


def test_transaction_failure_halfway_keeps_last_good_copy(sequence, tmp_path):
    mirror = LocalMirror(tmp_path / "latest.sqlite3")
    good = snapshot()
    mirror.update(good)
    a, _, _, _, members = sequence
    transfer(a, "TEST-SECOND-TRANSFER", members[-1].number, "1.00")
    with sqlite3.connect(mirror.path) as db:
        db.execute(
            "CREATE TRIGGER fail_write BEFORE INSERT ON ledger "
            "BEGIN SELECT RAISE(ABORT,'test interrupted'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        mirror.update(snapshot())
    assert mirror.read() == good
    assert mirror.status()["error_code"] == "IntegrityError"


@pytest.mark.parametrize(
    "change,code",
    [
        ("empty", "related_history_missing"),
        ("rewrite", "financial_history_changed"),
        ("identity", "membership_identity_changed"),
        ("source", "different_source"),
        ("old", "stale_snapshot"),
        ("context", "related_history_missing"),
    ],
)
def test_comparison_rejects_valid_but_unsafe_replacement(sequence, change, code):
    good = snapshot()
    bad = copy.deepcopy(good)
    if change == "empty":
        bad["records"] = []
    elif change == "rewrite":
        row(bad, "loyalty.ledger")["fields"]["reason"] = "rewritten history"
    elif change == "identity":
        row(bad, "loyalty.member")["fields"]["joined_at"] = "2026-10-01T00:00:00Z"
    elif change == "source":
        bad["source_id"] = "some-other-account"
    elif change == "old":
        bad["captured_at"] = "2020-10-10T00:00:00+03:00"
    elif change == "context":
        bad["records"].remove(row(bad, "accounts.customerprofile"))
    bad = seal(bad)
    # It passes its own arithmetic checks, but conflicts with the previous copy.
    validate(bad)
    with pytest.raises(BackupError) as error:
        compare(good, bad)
    assert error.value.code == code


def test_restore_refuses_default_database(sequence):
    with pytest.raises(BackupError) as error:
        restore_local(snapshot(), using="default")
    assert error.value.code == "restore_requires_separate_database"
    assert Ledger.objects.count() == 11


@pytest.fixture
def recovery_db(tmp_path, django_db_blocker):
    alias = "loyalty_recovery_test"
    connections.databases[alias] = {
        **connections["default"].settings_dict,
        "NAME": str(tmp_path / "recovery.sqlite3"),
    }
    try:
        with django_db_blocker.unblock():
            db = connections[alias]
            # Only the recovery subset, not the full-site migration history.
            # Full production migration replay remains a separate release gate.
            with db.schema_editor() as editor:
                for model in _models().values():
                    editor.create_model(model)
                from importlib import import_module

                install = import_module("apps.loyalty.migrations.0002_history_guards").install
                install(None, editor)
            yield alias
    finally:
        if hasattr(connections._connections, alias):
            connections[alias].close()
            del connections[alias]
        connections.databases.pop(alias, None)


def test_restore_into_separate_database_matches_every_row(sequence, recovery_db):
    data = snapshot()
    result = restore_local(data, using=recovery_db)
    assert result == validate(data)
    assert Ledger.objects.using(recovery_db).count() == 11
    assert Referral.objects.using(recovery_db).last().tree_number == "0001-0001-0002"
    restored = capture(source_id=data["source_id"], using=recovery_db)
    assert restored["records"] == data["records"]
    assert Ledger.objects.count() == 11
    assert [balance(m) for m in sequence[-1]] == list(map(Decimal, ["65", "103.5", "25.5", "39.5"]))
    with pytest.raises(BackupError) as error:
        restore_local(data, using=recovery_db)
    assert error.value.code == "restore_target_not_empty"


def test_capture_canonicalizes_only_redundant_datetime_zero_fraction(sequence, recovery_db):
    account = sequence[0]
    type(account).objects.filter(pk=account.pk).update(
        date_joined=datetime(2026, 10, 1, 8, 0, 0, 500, tzinfo=UTC)
    )
    data = snapshot()
    account_row = next(
        r for r in data["records"] if r["model"] == "auth.user" and r["pk"] == account.pk
    )
    assert account_row["fields"]["date_joined"] == "2026-10-01T08:00:00Z"
    restore_local(data, using=recovery_db)
    assert capture(source_id=data["source_id"], using=recovery_db)["records"] == data["records"]


@pytest.mark.parametrize(
    "model,field",
    [
        ("auth.user", "date_joined"),
        ("loyalty.member", "joined_at"),
        ("loyalty.ledger", "check_in_at"),
    ],
)
def test_legacy_zero_fraction_restore_and_history_checks(sequence, recovery_db, model, field):
    data = snapshot()
    legacy = copy.deepcopy(data)
    record = row(legacy, model)
    # A known same instant is used on both sides, not an arbitrary date change.
    if model == "auth.user":
        record["fields"][field] = "2026-10-10T13:00:00.000Z"
    else:
        record["fields"][field] = record["fields"][field][:-1] + ".000Z"
    legacy = seal(legacy)
    saved = copy.deepcopy(legacy)
    restore_local(legacy, using=recovery_db)
    recovered = capture(source_id=legacy["source_id"], using=recovery_db)
    compare(legacy, recovered)
    assert legacy == saved
    if model == "loyalty.ledger":
        changed = copy.deepcopy(recovered)
        row(changed, model)["fields"][field] = "2026-10-10T12:00:01Z"
        with pytest.raises(BackupError) as error:
            compare(legacy, seal(changed))
        assert error.value.code == "financial_history_changed"


def test_empty_initial_copy_is_valid_but_not_after_a_real_history(sequence, tmp_path):
    mirror = LocalMirror(tmp_path / "latest.sqlite3")
    nonempty = snapshot()
    empty = seal({**nonempty, "records": []})
    assert mirror.update(empty)["program_balance"] == "0.00"
    mirror.update(nonempty)
    with pytest.raises(BackupError):
        mirror.update(empty)


def test_local_mirror_cannot_be_used_with_postgres_source(monkeypatch, tmp_path):
    data = snapshot()
    monkeypatch.setattr(connections["default"], "vendor", "postgresql")
    with pytest.raises(BackupError) as error:
        LocalMirror(tmp_path / "latest.sqlite3").update(data)
    assert error.value.code == "local_rehearsal_only"
    assert not (tmp_path / "latest.sqlite3").exists()


@pytest.mark.parametrize("kind", ["account", "booking"])
def test_rehearsal_refuses_nonfictional_records(sequence, tmp_path, kind):
    data = snapshot()
    if kind == "account":
        row(data, "auth.user")["fields"]["email"] = "guest@example.com"
    else:
        row(data, "reservations.reservation")["fields"]["is_test"] = False
    with pytest.raises(BackupError):
        LocalMirror(tmp_path / "latest.sqlite3").update(seal(data))


def test_unknown_database_cannot_be_overwritten(tmp_path):
    path = tmp_path / "not-a-mirror.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE valuable (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO valuable VALUES (1)")
    with pytest.raises(BackupError) as error:
        LocalMirror(path).update(snapshot())
    assert error.value.code == "not_a_mirror_database"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id FROM valuable").fetchall() == [(1,)]


def test_restore_failure_rolls_back_all_inserted_rows(sequence, recovery_db):
    data = snapshot()
    row(data, "auth.user", 1)["fields"]["username"] = row(data, "auth.user")["fields"]["username"]
    with pytest.raises(IntegrityError):
        restore_local(seal(data), using=recovery_db)
    assert not any(m.objects.using(recovery_db).exists() for m in _models().values())
    assert Ledger.objects.count() == 11


def test_daily_dispatch_off_returns_without_touching_database(settings, monkeypatch):
    from apps.loyalty import tasks

    settings.LOYALTY_BACKUP_ENABLED = False
    monkeypatch.setattr(tasks.LocalMirror, "run", lambda **kw: pytest.fail("must not read source"))
    assert tasks.daily_backup_task.run() == {"status": "disabled"}


@pytest.mark.parametrize("debug,rehearsal", [(False, False), (False, True), (True, False)])
def test_daily_dispatch_has_no_unapproved_cloud_fallback(settings, debug, rehearsal, monkeypatch):
    from apps.loyalty import tasks

    settings.LOYALTY_BACKUP_ENABLED = True
    settings.DEBUG = debug
    settings.LOYALTY_BACKUP_LOCAL_REHEARSAL = rehearsal
    monkeypatch.setattr(tasks.LocalMirror, "run", lambda **kw: pytest.fail("must not read source"))
    with pytest.raises(ImproperlyConfigured):
        tasks.daily_backup_task.run()


def test_daily_local_dispatch_updates_same_copy_with_safe_status(sequence, settings, tmp_path):
    from apps.loyalty.tasks import daily_backup_task

    settings.LOYALTY_BACKUP_ENABLED = True
    settings.DEBUG = True
    settings.LOYALTY_BACKUP_LOCAL_REHEARSAL = True
    settings.LOYALTY_BACKUP_LOCAL_PATH = str(tmp_path / "latest.sqlite3")
    settings.LOYALTY_BACKUP_SOURCE_ID = "fictional-local-stage-ten"
    result = daily_backup_task.run()
    assert result["status"] == "succeeded"
    assert result["program_balance"] == "233.50"
    assert "records" not in result and "member_balances" not in result
    assert daily_backup_task.run() == result
    assert LocalMirror(settings.LOYALTY_BACKUP_LOCAL_PATH).status()["last_success"]


def test_daily_schedule_once_at_one_riyadh_and_off_by_default(settings):
    from scheduler_runtime.schedule import build_beat_schedule

    required = (
        "HOSTAWAY_AUTO_SYNC_ENABLED",
        "HOSTAWAY_AUTO_SYNC_INTERVAL_MINUTES",
        "HOSTAWAY_PRICE_CALENDAR_SYNC_ENABLED",
        "HOSTAWAY_PRICE_CALENDAR_SYNC_HOUR",
        "HOSTAWAY_PRICE_CALENDAR_SYNC_MINUTE",
        "HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED",
        "HOSTAWAY_WEBHOOK_PROCESSING_ENABLED",
        "HOSTAWAY_WEBHOOK_PROCESS_INTERVAL_MINUTES",
        "BOOKING_EXPIRATION_INTERVAL_MINUTES",
        "TRUSTINDEX_REVIEW_SYNC_ENABLED",
        "CELERY_SYNC_DISPATCH_ENABLED",
        "EMAIL_TASK_SCHEDULE_ENABLED",
    )
    values = {k: getattr(settings, k) for k in required}
    values["LOYALTY_BACKUP_ENABLED"] = False
    old = build_beat_schedule(values)
    assert "loyalty-daily-backup" not in old
    values["LOYALTY_BACKUP_ENABLED"] = True
    enabled = build_beat_schedule(values)
    assert {k: v for k, v in enabled.items() if k != "loyalty-daily-backup"} == old
    entry = enabled["loyalty-daily-backup"]
    assert entry["schedule"].hour == {1}
    assert entry["schedule"].minute == {0}
    assert settings.CELERY_TIMEZONE == "Asia/Riyadh"


def test_snapshot_rejects_unknown_source_identity():
    with pytest.raises(BackupError):
        capture(source_id="")


def test_source_read_failure_is_recorded_without_losing_last_copy(sequence, tmp_path, monkeypatch):
    from apps.loyalty import backup

    mirror = LocalMirror(tmp_path / "latest.sqlite3")
    data = snapshot()
    mirror.update(data)

    def fail(**kwargs):
        raise RuntimeError("private-details-must-not-be-logged")

    monkeypatch.setattr(backup, "capture", fail)
    with pytest.raises(RuntimeError):
        mirror.run(source_id=data["source_id"])
    assert mirror.read() == data
    status = mirror.status()
    assert status["last_success"]
    assert status["error_code"] == "RuntimeError"
    assert "private-details" not in str(status)


def test_deferred_corrections_and_receipt_state_are_captured(sequence):
    member = sequence[-1][0]
    member.deferred_corrections = [{"value": "4.50", "reference": "TEST-ORIGINAL-EARNING"}]
    member.save(update_fields=["deferred_corrections"])
    data = snapshot()
    assert (
        row(data, "loyalty.member")["fields"]["deferred_corrections"] == member.deferred_corrections
    )


def test_pg_capture_refuses_an_existing_transaction(monkeypatch):
    monkeypatch.setattr(connections["default"], "vendor", "postgresql")
    assert connections["default"].in_atomic_block
    with pytest.raises(BackupError) as error:
        snapshot()
    assert error.value.code == "snapshot_requires_own_transaction"
