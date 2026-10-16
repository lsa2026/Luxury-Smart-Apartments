"""Fictional member trees only. No live linking, awards, messages or provider calls."""

import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import profile_for
from apps.loyalty.backup import (
    LocalMirror,
    _schema_hash,
    capture,
    compare,
    restore_local,
    seal,
    validate,
)
from apps.loyalty.models import Ledger, Referral, ReferralRevision
from apps.loyalty.referral_ui import search_members
from apps.loyalty.referrals import graph, link_members
from apps.loyalty.services import ancestors, balance, reconcile, statement
from tests.test_loyalty_stage8 import booking, join, user
from tests.test_loyalty_stage10 import recovery_db as recovery_db

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def programme(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "synthetic-admin-link"
    settings.LOYALTY_TERMS_REFERENCE = "/synthetic-terms/"
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = False
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    return clock


@pytest.fixture
def operator():
    return get_user_model().objects.create_superuser(
        username="referral-owner", email="referral-owner@example.invalid"
    )


def member(n):
    m = join(user(n))
    # Synthetic, trusted completed-stay eligibility; no live exception or booking mutation.
    m.first_completed_at = timezone.now() - timedelta(days=1)
    m.save(update_fields=["first_completed_at"])
    return m


def link(operator, child, parent, *, operation=None):
    return link_members(
        operator,
        child.pk,
        parent.pk,
        reason="فقد رابط الدعوة وتم التحقق من الطرفين",
        operation=operation or uuid.uuid4(),
    )


def endpoint():
    return reverse("loyalty:referrals")


def client_for(operator):
    client = Client()
    client.force_login(operator)
    return client


def review(client, child, parent):
    response = client.post(
        endpoint(),
        {
            "action": "review",
            "member": child.pk,
            "inviter": parent.pk,
            "reason": "تأكيد الدعوة من الطرفين",
        },
    )
    assert response.status_code == 200
    return response.context["confirmation"]["token"]


def test_link_root_with_descendants_updates_current_tree_without_rewriting_history(operator):
    a, b, c, d, e = [member(n) for n in range(5)]
    link(operator, b, a)
    link(operator, d, c)
    originals = list(Referral.objects.values())
    old_d = graph()[d.pk].tree_number
    with patch("httpx.Client.send", side_effect=AssertionError("No outbound work")):
        link(operator, c, b)
        link(operator, e, b)  # Already-invited b may invite any number.
    assert list(Referral.objects.values()) == originals
    assert [m.pk for m in ancestors(d)] == [c.pk, b.pk, a.pk]
    assert graph()[d.pk].tree_number != old_d
    assert graph()[d.pk].tree_number.startswith(graph()[c.pk].tree_number + "-")
    assert statement(d.customer)["tree_number"] == graph()[d.pk].tree_number
    assert not Ledger.objects.exists()
    # New ordinary signup under a moved member follows the CURRENT tree.
    c.invitation_token = "synthetic-c-token"
    c.save(update_fields=["invitation_token"])
    f = join(user("new"), invitation_token=c.invitation_token, confirmed_inviter=c.pk)
    assert graph()[f.pk].tree_number.startswith(graph()[c.pk].tree_number + "-")
    assert [m.pk for m in ancestors(f)] == [c.pk, b.pk, a.pk]
    validate(capture(source_id="synthetic-referral-test"))


def test_past_booking_keeps_old_chain_but_new_booking_rewards_new_inviter(operator):
    a, b, c = [member(n) for n in range(3)]
    link(operator, c, b)
    old = booking(c.customer)
    reconcile(old.pk)
    old.refresh_from_db()
    # Exercise pre-release settlements without the newly added snapshot as well.
    old.loyalty_state.pop("referral_chain")
    old.save(update_fields=["loyalty_state"])
    before = list(Ledger.objects.values())
    prior_balances = [balance(m) for m in (a, b, c)]
    link(operator, b, a)
    assert list(Ledger.objects.values()) == before
    assert [balance(m) for m in (a, b, c)] == prior_balances
    old.refresh_from_db()
    assert old.loyalty_state["referral_chain"] == [b.pk]
    assert reconcile(old.pk)["movements"] == 0
    # Subsequent price reduction of old accrual must not create new-ancestor rewards.
    from apps.payments.models import PaymentAttempt
    from tests.test_loyalty_settlement_integration import change

    modification = change(old, total="600.00")
    modification.quote_snapshot = {
        "components": [{"type": "baseRate", "total": "600.00", "included_in_total": True}]
    }
    modification.save(update_fields=["quote_snapshot"])
    old.total_price = Decimal("600.00")
    old.save(update_fields=["total_price"])
    q = old.booking_intent.quote
    q.total_price = Decimal("600.00")
    q.components = [{"type": "baseRate", "total": "600.00", "included_in_total": True}]
    q.save()
    assert reconcile(old.pk)["status"] == "settled"
    assert balance(a) == 0
    assert balance(b) == Decimal("30.00")
    assert balance(c) == Decimal("18.00")
    new = booking(c.customer)
    reconcile(new.pk)
    assert balance(a) == Decimal("7.50")
    assert balance(b) == Decimal("67.50")
    assert (
        Ledger.objects.filter(reservation=new, member=b).get().tree_number
        == graph()[b.pk].tree_number
    )
    assert PaymentAttempt.objects.count() == 2
    validate(capture(source_id="synthetic-referral-test"))


def test_duplicate_request_is_idempotent_but_payload_cannot_be_reused(operator):
    a, b, c = [member(n) for n in range(3)]
    operation = uuid.uuid4()
    result = link(operator, b, a, operation=operation)
    assert link(operator, b, a, operation=operation).pk == result.pk
    assert ReferralRevision.objects.count() == 1
    with pytest.raises(ValidationError):
        link(operator, b, c, operation=operation)


def test_reject_self_second_inviter_cycle_inactive_and_unqualified(operator):
    a, b, c, d = [member(n) for n in range(4)]
    with pytest.raises(ValidationError):
        link(operator, a, a)
    link(operator, b, a)
    for child, parent in [(b, c), (a, b), (b, a)]:
        with pytest.raises(ValidationError):
            link(operator, child, parent)
    c.status = "withdrawn"
    c.save(update_fields=["status"])
    with pytest.raises(ValidationError):
        link(operator, c, a)
    d.first_completed_at = None
    d.save(update_fields=["first_completed_at"])
    with pytest.raises(ValidationError):
        link(operator, c, d)
    assert ReferralRevision.objects.count() == 1


def test_read_only_search_review_then_explicit_confirm_and_retry(operator):
    a, b = [member(n) for n in range(2)]
    client = client_for(operator)
    profile = profile_for(b.customer)
    profile.phone = "+1 (646) 281-7246"
    profile.save(update_fields=["phone"])
    assert search_members("0016462817246") == [b]
    assert search_members(b.number.lower()) == [b]
    response = client.get(endpoint(), {"member_q": "0016462817246", "inviter_q": a.number})
    assert list(response.context["member_matches"]) == [b]
    assert response.headers["Content-Language"] == "ar"
    assert "مراجعة الربط قبل التأكيد" in response.content.decode()
    token = review(client, b, a)
    assert ReferralRevision.objects.count() == 0
    missing_check = client.post(endpoint(), {"action": "confirm", "confirmation": token})
    assert missing_check.status_code == 200 and not ReferralRevision.objects.exists()
    data = {"action": "confirm", "confirmation": token, "verified": "on"}
    assert client.post(endpoint(), data).status_code == 302
    assert client.post(endpoint(), data).status_code == 302
    assert ReferralRevision.objects.count() == 1
    assert not Ledger.objects.exists()


def test_expired_tampered_and_changed_tree_confirmation_refused(operator, programme):
    a, b, c, d = [member(n) for n in range(4)]
    client = client_for(operator)
    token = review(client, b, a)
    data = {"action": "confirm", "confirmation": token + "bad", "verified": "on"}
    assert client.post(endpoint(), data).status_code == 200
    assert not ReferralRevision.objects.exists()
    data["confirmation"] = token
    link(operator, d, c)
    assert client.post(endpoint(), data).status_code == 200
    assert graph()[b.pk].inviter_id is None
    token = review(client, b, a)
    with patch("django.core.signing.time.time", return_value=time.time() + 601):
        data["confirmation"] = token
        assert client.post(endpoint(), data).status_code == 200
    assert graph()[b.pk].inviter_id is None


def test_restricted_owner_pilot_csrf_and_activation_gates(operator, settings):
    a, b = [member(n) for n in range(2)]
    guest = client_for(a.customer)
    assert guest.get(endpoint()).status_code in (302, 403)
    a.customer.is_staff = True
    a.customer.save(update_fields=["is_staff"])
    assert guest.get(endpoint()).status_code == 403
    with pytest.raises(PermissionDenied):
        link(a.customer, b, a)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(operator)
    assert strict.post(endpoint(), {"action": "review"}).status_code == 403
    settings.LOYALTY_PILOT_ONLY = True
    settings.LOYALTY_PILOT_EMAILS = [a.customer.email]
    with pytest.raises(ValidationError):
        link(operator, b, a)
    assert not search_members(b.number)
    settings.LOYALTY_UI_ENABLED = False
    assert client_for(operator).get(endpoint()).status_code == 404


def test_history_is_immutable_and_atomic_failure_leaves_no_half_tree(operator):
    a, b, c = [member(n) for n in range(3)]
    link(operator, c, b)
    count = ReferralRevision.objects.count()
    create = ReferralRevision.objects.create
    calls = []

    def fail_second(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise ValidationError("synthetic write failure")
        return create(**kwargs)

    with patch.object(ReferralRevision.objects, "create", side_effect=fail_second):
        with pytest.raises(ValidationError):
            link(operator, b, a)
    assert ReferralRevision.objects.count() == count
    assert graph()[b.pk].inviter_id is None
    row = ReferralRevision.objects.first()
    with pytest.raises(ValidationError):
        row.save()
    with pytest.raises(ValidationError):
        ReferralRevision.objects.all().delete()
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE loyalty_referralrevision SET reason='changed' WHERE id=%s", [row.pk])


def test_backup_preserves_revision_history_and_accepts_previous_schema(operator, tmp_path):
    a, b = [member(n) for n in range(2)]
    before = capture(source_id="synthetic-referral-test")
    old = seal({**before, "schema": _schema_hash(referral_revisions=False)})
    validate(old)
    link(operator, b, a)
    after = capture(source_id="synthetic-referral-test")
    compare(old, after)
    mirror = LocalMirror(tmp_path / "referrals.sqlite3")
    mirror.update(old)
    mirror.update(after)
    assert mirror.read()["digest"] == after["digest"]
    bad = {**after, "records": [dict(r) for r in after["records"]]}
    row = next(r for r in bad["records"] if r["model"] == "loyalty.referralrevision")
    row["fields"] = {**row["fields"], "reason": "rewritten"}
    with pytest.raises(ValidationError):
        compare(after, seal(bad))


@pytest.mark.skipif(connection.vendor != "sqlite", reason="Separate local SQLite recovery only")
def test_restore_keeps_original_paths_revisions_and_both_ledger_generations(operator, request):
    recovery_alias = request.getfixturevalue("recovery_db")
    a, b = [member(n) for n in range(2)]
    reconcile(booking(b.customer).pk)
    link(operator, b, a)
    reconcile(booking(b.customer).pk)
    snapshot = capture(source_id="synthetic-referral-test")
    assert restore_local(snapshot, using=recovery_alias) == validate(snapshot)
    assert (
        capture(source_id=snapshot["source_id"], using=recovery_alias)["records"]
        == snapshot["records"]
    )


def test_empty_and_ineligible_search_cannot_link_or_bypass_qualification(operator):
    a, b = [member(n) for n in range(2)]
    a.first_completed_at = None
    a.save(update_fields=["first_completed_at"])
    with pytest.raises(ValidationError, match="أهلية الدعوة"):
        link(operator, b, a)
    client = client_for(operator)
    response = client.get(endpoint(), {"member_q": "0501234567", "inviter_q": a.number})
    assert not response.context["member_matches"]
    assert "لم نجد عضوية نشطة مطابقة" in response.content.decode()
    assert not ReferralRevision.objects.exists()
