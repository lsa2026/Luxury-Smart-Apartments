"""Offline fixtures only: timed eligibility, staff override and once-only posting."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone, translation

from apps.loyalty.accrual import TERMS_REFERENCE, TERMS_URL_VERSION, TERMS_VERSION
from apps.loyalty.booking_operations import stay_controls
from apps.loyalty.models import Ledger
from apps.loyalty.public_terms import (
    CONTENT,
    URL_VERSION,
    VERSION,
    consent_metadata,
    document,
    terms_reference,
)
from apps.loyalty.services import balance, confirm_arrival, issue_invite, reconcile
from apps.loyalty.tasks import accrue_due_stays_task
from apps.payments.models import PaymentAttempt
from apps.reservations.models import Reservation
from tests.test_loyalty_stage8 import booking, join, payment, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def policy(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_PILOT_ONLY = False
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = TERMS_VERSION
    settings.LOYALTY_TERMS_REFERENCE = TERMS_REFERENCE
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = False
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = False
    clock = [datetime(2026, 10, 16, 11, 59, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    return clock


def stay(*, paid=True, joined=True):
    guest = user("scheduled")
    member = join(guest) if joined else None
    return booking(guest, arrived=False, paid=paid), member


def owner():
    return get_user_model().objects.create_superuser(username="scheduled-owner")


def test_at_three_pm_awards_once_without_fabricating_physical_arrival(policy):
    r, member = stay()
    assert reconcile(r.pk)["status"] == "arrival_required"
    assert accrue_due_stays_task()["settled"] == 0
    assert balance(member) == 0
    policy[0] += timedelta(minutes=1)
    assert accrue_due_stays_task()["settled"] == 1
    assert accrue_due_stays_task()["settled"] == 0
    assert reconcile(r.pk)["status"] == "unchanged"
    r.refresh_from_db()
    assert "arrival" not in r.loyalty_state
    assert r.loyalty_state["scheduled_accrual"]["at"] == "2026-10-16T15:00:00+03:00"
    row = Ledger.objects.get(member=member)
    assert row.actual_arrival_at is None
    assert row.evidence["arrival"]["at"] is None
    assert "scheduled-checkin:" in row.arrival_reference
    assert balance(member) == Decimal("22.50")
    with pytest.raises(ValidationError):
        issue_invite(member.customer, r.pk)


@pytest.mark.parametrize("zone", ["Asia/Riyadh", "Africa/Casablanca"])
def test_property_local_time_not_server_or_guest_time(policy, zone):
    r, member = stay()
    r.property.time_zone_name = zone
    r.property.save(update_fields=["time_zone_name"])
    boundary = datetime.combine(r.check_in, datetime.min.time(), ZoneInfo(zone)).replace(hour=15)
    policy[0] = (boundary - timedelta(seconds=1)).astimezone(UTC)
    assert accrue_due_stays_task()["movements"] == 0
    policy[0] = boundary.astimezone(UTC)
    assert accrue_due_stays_task()["movements"] == 1
    assert balance(member) == Decimal("22.50")


def test_future_december_booking_manual_confirmation_awards_now(policy):
    r, member = stay()
    r.check_in, r.check_out = datetime(2026, 12, 15).date(), datetime(2026, 12, 16).date()
    r.save(update_fields=["check_in", "check_out"])
    administrator = owner()
    assert stay_controls(administrator, r)["can_arrive"]
    assert accrue_due_stays_task()["movements"] == 0
    assert (
        confirm_arrival(administrator, r.pk, arrived_at=policy[0], reference="staff-now")[
            "movements"
        ]
        == 1
    )
    assert balance(member) == Decimal("22.50")
    policy[0] = datetime(2026, 12, 15, 12, tzinfo=UTC)
    assert accrue_due_stays_task()["movements"] == 0
    assert reconcile(r.pk)["movements"] == 0
    assert Ledger.objects.count() == 1


def test_automatic_then_staff_confirmation_does_not_post_again(policy):
    r, member = stay()
    policy[0] += timedelta(minutes=2)
    accrue_due_stays_task()
    assert (
        confirm_arrival(owner(), r.pk, arrived_at=policy[0], reference="real-arrival")["movements"]
        == 0
    )
    assert balance(member) == Decimal("22.50") and Ledger.objects.count() == 1


def test_guest_cannot_confirm_arrival_and_admin_cannot_submit_a_future_timestamp(policy):
    r, member = stay()
    with pytest.raises(PermissionDenied):
        confirm_arrival(member.customer, r.pk, arrived_at=policy[0], reference="forged")
    with pytest.raises(ValidationError):
        confirm_arrival(
            owner(), r.pk, arrived_at=policy[0] + timedelta(minutes=1), reference="future"
        )
    assert not Ledger.objects.exists()


def test_partial_verified_payment_still_does_not_award(policy):
    r, _ = stay(paid=False)
    payment(r, Decimal("500.00"))
    policy[0] += timedelta(minutes=2)
    assert accrue_due_stays_task()["movements"] == 0
    assert reconcile(r.pk)["status"] == "verified_payment_incomplete"
    assert not Ledger.objects.exists()


@pytest.mark.parametrize("admin", [False, True])
def test_unpaid_never_awards_then_verified_payment_event_qualifies(policy, admin):
    r, member = stay(paid=False)
    if admin:
        confirm_arrival(owner(), r.pk, arrived_at=policy[0], reference="staff-unpaid")
    policy[0] += timedelta(minutes=2)
    accrue_due_stays_task()
    assert not Ledger.objects.exists()
    payment(r, r.total_price, verified=False)
    accrue_due_stays_task()
    assert not Ledger.objects.exists()
    p = PaymentAttempt.objects.get(booking_intent=r.booking_intent)
    p.verified_at = policy[0]
    p.save(update_fields=["verified_at"])
    assert reconcile(r.pk)["movements"] == 1
    assert balance(member) == Decimal("22.50")


def test_enrolment_during_current_paid_stay_reevaluates_it(policy):
    r, _ = stay(joined=False)
    policy[0] += timedelta(minutes=2)
    assert accrue_due_stays_task()["movements"] == 0
    member = join(r.booking_intent.customer)
    assert balance(member) == Decimal("22.50")
    assert accrue_due_stays_task()["movements"] == 0


@pytest.mark.parametrize("reason", ["cancelled", "external", "prelaunch", "withdrawn", "finished"])
def test_scheduler_does_not_award_out_of_scope_or_old_finished_stays(policy, reason):
    r, member = stay()
    if reason == "cancelled":
        r.normalized_status = Reservation.Status.CANCELLED
    elif reason == "external":
        r.source_type = Reservation.SourceType.HOSTAWAY_MANUAL
    elif reason == "prelaunch":
        Reservation.objects.filter(pk=r.pk).update(created_at=datetime(2026, 9, 1, tzinfo=UTC))
    elif reason == "withdrawn":
        member.status = "withdrawn"
        member.save(update_fields=["status"])
    elif reason == "finished":
        r.check_in -= timedelta(days=5)
        r.check_out -= timedelta(days=5)
    r.save(update_fields=["normalized_status", "source_type", "check_in", "check_out"])
    policy[0] += timedelta(minutes=2)
    assert accrue_due_stays_task()["movements"] == 0
    assert not Ledger.objects.exists()


def test_invalid_property_zone_does_not_stop_later_bookings(policy):
    r, _ = stay()
    r.property.time_zone_name = ""
    r.property.save(update_fields=["time_zone_name"])
    policy[0] += timedelta(minutes=2)
    assert accrue_due_stays_task()["movements"] == 0
    assert not Ledger.objects.exists()


@pytest.mark.parametrize("flag", ["disabled", "unapproved", "old_terms"])
def test_worker_is_noop_until_policy_enabled(policy, settings, flag):
    stay()
    policy[0] += timedelta(minutes=2)
    if flag == "disabled":
        settings.LOYALTY_ENABLED = False
    elif flag == "unapproved":
        settings.LOYALTY_TERMS_APPROVED = False
    else:
        settings.LOYALTY_TERMS_VERSION = VERSION
    assert accrue_due_stays_task()["status"] == "disabled"
    assert not Ledger.objects.exists()


def test_cancellation_after_automatic_accrual_corrects_but_preserves_original(policy):
    r, member = stay()
    policy[0] += timedelta(minutes=2)
    accrue_due_stays_task()
    original = Ledger.objects.get()
    snapshot = original.evidence.copy()
    r.refresh_from_db()
    from apps.loyalty.stay import record_departure

    # Operational admission now governs cancellation too. An explicit same-day
    # exit before 20:00 settles all unused nights without faking physical arrival.
    r.booking_intent.quote.night_prices = {
        "currency": "SAR",
        "reference": "fictional-two-night-contract",
        "nights": [
            {"date": (r.check_in + timedelta(days=n)).isoformat(), "value": "375.00"}
            for n in range(2)
        ],
    }
    r.booking_intent.quote.save(update_fields=["night_prices"])
    record_departure(owner(), r.pk, vacated_at=policy[0], reference="fictional-exit")
    r.refresh_from_db()
    r.normalized_status = Reservation.Status.CANCELLED
    r.save(update_fields=["normalized_status"])
    assert reconcile(r.pk)["status"] == "settled"
    assert balance(member) == 0
    original.refresh_from_db()
    assert original.evidence == snapshot and original.credit == Decimal("22.50")
    assert Ledger.objects.filter(original=original, debit=Decimal("22.50")).exists()


def test_automatic_evidence_survives_checkout_for_later_reconciliation(policy):
    r, member = stay()
    policy[0] += timedelta(minutes=2)
    accrue_due_stays_task()
    policy[0] += timedelta(days=3)
    assert reconcile(r.pk)["status"] == "unchanged"
    assert balance(member) == Decimal("22.50")


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_second_edition_is_published_without_mutating_first_or_old_consents(language):
    c = Client()
    old = c.get(f"/{language}/legal/rewards/{URL_VERSION}/")
    new = c.get(f"/{language}/legal/rewards/{TERMS_URL_VERSION}/")
    assert old.status_code == new.status_code == 200
    old_hash = hashlib.sha256(
        json.dumps(CONTENT[language], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    assert (
        old_hash
        == {
            "ar": "6147ef2c8e2eef4078b311a55cc40249536f9e649153f638436fea23f77e1217",
            "en": "5c014d8d657463f68025b05b573daf719defded48ba2a7b4d4219bbe4c550de3",
            "fr": "f790a0a2718a5ab652db9aa2f7a3650e53d72837a216bbd742fd98d6d517dd0a",
        }[language]
    )
    assert old["X-Document-SHA256"] == old_hash
    assert new["X-Document-SHA256"] != old_hash
    assert new["X-Document-Version"] == TERMS_VERSION
    with translation.override(language):
        url = f"/{language}/legal/rewards/{TERMS_URL_VERSION}/"
        assert terms_reference() == url
        assert (
            consent_metadata(TERMS_VERSION, url, language)["terms_content_sha256"]
            == (document(language)["sha256"])
        )
    assert document(language, version=VERSION)["sha256"] == old_hash


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_new_enrolment_records_new_edition_without_touching_old_member(language, settings):
    old_guest = user("old-consent")
    old_member = join(old_guest)
    old_history = old_member.consent_history.copy()
    guest = user("new-consent")
    c = Client()
    c.force_login(guest)
    c.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = c.post("/my-bookings/rewards/", {"action": "join", "consent": "on"})
    assert response.status_code == 302
    guest.refresh_from_db()
    assert guest.member.terms_version == TERMS_VERSION
    assert guest.member.consent_history[0]["terms_content_sha256"] == document(language)["sha256"]
    old_member.refresh_from_db()
    assert old_member.consent_history == old_history


@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires isolated PostgreSQL")
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("manual_second", [True, False])
def test_parallel_automatic_and_staff_paths_post_one_reward(policy, manual_second):
    r, member = stay()
    administrator = owner()
    policy[0] += timedelta(minutes=2)

    def execute(manual):
        close_old_connections()
        try:
            if manual:
                return confirm_arrival(
                    administrator, r.pk, arrived_at=policy[0], reference="concurrent-staff"
                )
            return accrue_due_stays_task()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(execute, [False, manual_second]))
    assert balance(member) == Decimal("22.50")
    assert Ledger.objects.filter(member=member).count() == 1
