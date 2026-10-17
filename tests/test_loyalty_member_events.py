"""Lifecycle history and owner-only closure; fictional accounts, no external writes."""

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib import import_module

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.loyalty.backup import BackupError, _schema_hash, capture, compare, seal, validate
from apps.loyalty.events import record
from apps.loyalty.membership_ui import close_membership, state
from apps.loyalty.models import GuestEvent, Ledger
from apps.loyalty.services import balance, reconcile, transfer
from apps.notifications.models import AuditLog
from tests.test_loyalty_stage8 import booking, join, user

pytestmark = pytest.mark.django_db
URL = "/admin/loyalty/memberships/"


@pytest.fixture(autouse=True)
def programme(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_PILOT_ONLY = settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = False
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = False
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    return clock


@pytest.fixture
def owner():
    return get_user_model().objects.create_superuser(
        username="event-owner", email="event-owner@example.invalid"
    )


@pytest.fixture
def admin(owner):
    c = Client()
    c.force_login(owner)
    return c


def confirm(admin, member, **changes):
    page = admin.get(URL, {"customer": member.customer_id})
    payload = {
        "customer": member.customer_id,
        "snapshot": page.context["closure_form"].initial["snapshot"],
        "reason": "طلب الضيف إلغاء العضوية بعد التحقق من هويته",
        "identity_confirmed": "on",
        "forfeiture_confirmed": "on",
    }
    payload.update(changes)
    return admin.post(URL, payload)


def test_registration_email_proof_and_join_are_separate_events():
    u = user("lifecycle")
    initial = GuestEvent.objects.filter(customer=u)
    assert set(initial.values_list("kind", flat=True)) == {"account_created", "email_verified"}
    assert all(e.member_id is None for e in initial)
    member = join(u)
    assert join(u).pk == member.pk
    event = initial.get(kind="joined")
    assert event.member == member and event.actor == u
    assert event.evidence["terms_version"] == member.terms_version
    assert GuestEvent.objects.filter(customer=u).count() == 3
    assert Ledger.objects.count() == 0


def test_phone_proof_is_not_contact_save_and_does_not_store_secrets():
    u = user("phone")
    binding = VerifiedPhone.objects.create(user=u, phone="+12125551234")
    event = GuestEvent.objects.get(reference=f"phone-proof:{binding.version}")
    assert event.after == {"phone": binding.phone}
    assert event.actor == u and event.occurred_at == binding.verified_at
    assert not any("code" in key or "token" in key for key in event.evidence)


def test_profile_contact_save_is_audited_but_not_verification(client):
    u = user("profile")
    client.force_login(u)
    response = client.post(
        "/my-bookings/details/",
        {
            "first_name": "ضيف",
            "last_name": "افتراضي",
            "phone_country": "US",
            "phone": "+12125551234",
            "preferred_language": "ar",
            "residence_country": "US",
            "residence_city": "مدينة افتراضية",
        },
    )
    assert response.status_code == 302
    event = GuestEvent.objects.get(customer=u, kind="profile_changed")
    assert event.before["phone"] == "" and event.after["phone"] == "+12125551234"
    assert not GuestEvent.objects.filter(customer=u, kind="phone_verified").exists()


def test_membership_admin_cannot_be_disabled_via_guest_actions(client):
    member = join(user("self-close"))
    client.force_login(member.customer)
    client.post("/my-bookings/rewards/", {"action": "withdraw", "member": member.pk})
    member.refresh_from_db()
    assert member.status == "active"
    assert not GuestEvent.objects.filter(member=member, kind="closed").exists()


def test_identity_audit_is_bridged_without_mutating_original(owner):
    u = user("identity")
    member = join(u)
    row = AuditLog.objects.create(
        actor_user=owner,
        action="guest.identity_changed",
        object_type="guest_account",
        object_reference=str(u.pk),
        summary="طلب تعديل",
        metadata={
            "old_email": u.email,
            "new_email": "new@example.invalid",
            "old_phone": "+12125551234",
            "new_phone": "+12125551235",
            "reason": "طلب الضيف",
            "phone_requires_whatsapp_proof": True,
        },
    )
    event = GuestEvent.objects.get(reference=f"identity:{row.pk}")
    assert event.member == member and event.actor == owner and event.source == "admin"
    assert event.before["email"] == u.email and event.after["phone"] == "+12125551235"
    assert event.evidence["phone_requires_proof"] is True
    row.refresh_from_db()
    assert row.metadata["old_email"] == u.email


def test_zero_balance_closure_is_audited_without_fake_points(admin, owner):
    member = join(user("zero"))
    response = confirm(admin, member)
    assert response.status_code == 302
    member.refresh_from_db()
    assert member.status == "withdrawn" and member.customer.is_active
    event = GuestEvent.objects.get(member=member, kind="closed")
    assert event.actor == owner and event.reason and event.source == "admin"
    assert event.ledger_id is None and event.after["balance"] == "0.00"
    assert Ledger.objects.count() == 0


def test_positive_closure_links_exact_forfeiture_and_keeps_gift(admin, programme):
    a, b = user("sender"), user("recipient")
    am, bm = join(a), join(b)
    r = booking(a, "957.00")
    reconcile(r.pk)
    transfer(a, "gift-for-closure", bm.number, "3.00")
    programme[0] += timedelta(days=3)
    old_rows = list(Ledger.objects.values("pk", "credit", "debit", "member_balance_after"))
    assert confirm(admin, am).status_code == 302
    event = GuestEvent.objects.get(member=am, kind="closed")
    assert event.ledger.debit == Decimal("25.71") and event.ledger.event_type == "withdraw"
    assert event.before["balance"] == "25.71" and event.after["balance"] == "0.00"
    assert balance(am) == 0 and balance(bm) == 3
    assert (
        list(
            Ledger.objects.order_by("sequence").values(
                "pk", "credit", "debit", "member_balance_after"
            )
        )[:-1]
        == old_rows
    )
    r.refresh_from_db()
    assert r.normalized_status == "confirmed"
    assert confirm_without_form(admin, am).status_code == 200
    assert GuestEvent.objects.filter(member=am, kind="closed").count() == 1


def confirm_without_form(admin, member):
    return admin.post(URL, {"customer": member.customer_id, "reason": "طلب مكرر"})


def test_negative_balance_not_forgiven(admin):
    from tests.test_loyalty_signed_balances import cancel

    # Use the same production reconciliation path, never manufacture ledger balances.
    a, b = user("negative"), user("keep-gift")
    am, bm = join(a), join(b)
    r = booking(a, "957.00")
    reconcile(r.pk)
    transfer(a, "gift-before-reversal", bm.number, "3.00")
    cancel(r)
    assert balance(am) == -3
    assert confirm(admin, am).status_code == 302
    event = GuestEvent.objects.get(member=am, kind="closed")
    assert event.after["balance"] == "-3.00" and event.ledger_id is None
    assert balance(am) == -3 and balance(bm) == 3


@pytest.mark.parametrize("missing", ["identity_confirmed", "forfeiture_confirmed", "reason"])
def test_required_confirmations_do_not_close_account(admin, missing):
    member = join(user(missing))
    assert confirm(admin, member, **{missing: ""}).status_code == 200
    member.refresh_from_db()
    assert member.status == "active"
    assert not GuestEvent.objects.filter(member=member, kind="closed").exists()


def test_active_booking_blocks_closure(admin):
    member = join(user("future"))
    booking(member.customer, paid=False, arrived=False)
    response = confirm(admin, member)
    assert response.status_code == 200
    assert "حجز قائم" in response.content.decode()
    member.refresh_from_db()
    assert member.status == "active"


def test_tampered_and_cross_member_snapshot_rejected(admin):
    a, b = join(user("tamper-a")), join(user("tamper-b"))
    proof = admin.get(URL, {"customer": b.customer_id}).context["closure_form"].initial["snapshot"]
    assert confirm(admin, a, snapshot=proof).status_code == 200
    assert confirm(admin, a, snapshot="bad-token").status_code == 200
    assert not GuestEvent.objects.filter(kind="closed").exists()


def test_stale_snapshot_rejected(owner):
    member = join(user("stale"))
    before = state(member)
    member.invitation_issued_at = timezone.now()
    member.updated_at += timedelta(seconds=1)
    # auto_now would use the fixed test clock, so change a signed state value instead.
    member.status = "withdrawn"
    member.save(update_fields=["status"])
    with pytest.raises(ValidationError):
        close_membership(owner, member.pk, reason="طلب موثق", expected=before, reference="stale")


def test_guests_and_regular_staff_have_no_access(client):
    for u in (
        user("guest-denied"),
        get_user_model().objects.create_user(username="staff-denied", is_staff=True),
    ):
        client.force_login(u)
        assert client.get(URL).status_code in {302, 403}
        assert client.post(URL, {"customer": u.pk}).status_code in {302, 403}
        with pytest.raises(PermissionDenied):
            close_membership(u, 1, reason="غير مخول", expected=[], reference="deny")


def test_csrf_required_and_get_never_closes(owner):
    member = join(user("csrf"))
    c = Client(enforce_csrf_checks=True)
    c.force_login(owner)
    assert c.post(URL, {"customer": member.customer_id}).status_code == 403
    assert c.get(URL, {"customer": member.customer_id, "action": "close"}).status_code == 200
    member.refresh_from_db()
    assert member.status == "active"


def test_old_phone_search_finds_history_and_arabic_mobile_page(admin, owner):
    u = user("search")
    join(u)
    AuditLog.objects.create(
        actor_user=owner,
        action="guest.identity_changed",
        object_type="guest_account",
        object_reference=str(u.pk),
        summary="تغيير الجوال",
        metadata={"old_phone": "+12125551234", "new_phone": "+12125551235"},
    )
    page = admin.get(URL, {"q": "0012125551234"}, HTTP_ACCEPT_LANGUAGE="en")
    assert page.status_code == 200 and page["Content-Language"] == "ar"
    assert page.context["matches"] == [u]
    assert page.context["event_page"].paginator.count == 4
    assert page["Cache-Control"].find("no-store") >= 0
    assert 'dir="rtl"' in page.content.decode()


def test_event_filters_pagination_and_no_public_self_closure(admin):
    member = join(user("pages"))
    for n in range(30):
        record(
            customer=member.customer,
            member=member,
            kind="email_verified",
            reference=f"proof-page:{n}",
            source="guest",
        )
    page = admin.get(URL, {"customer": member.customer_id, "kind": "email_verified", "page": 2})
    assert len(page.context["event_page"]) == 6
    assert page.context["event_page"].paginator.count == 31
    assert admin.get("/admin/loyalty/").content.decode().find("أحداث الضيوف") >= 0


def test_event_immutable_in_model_queryset_and_database():
    event = GuestEvent.objects.get(customer=user("immutable"), kind="account_created")
    with pytest.raises(ValidationError):
        event.save()
    with pytest.raises(ValidationError):
        GuestEvent.objects.filter(pk=event.pk).update(reason="tamper")
    with pytest.raises(ValidationError):
        event.delete()
    for sql in (
        "UPDATE loyalty_guestevent SET reason='tamper' WHERE id=%s",
        "DELETE FROM loyalty_guestevent WHERE id=%s",
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [event.pk])


def test_event_member_and_financial_owner_checked():
    a, b = user("owner-a"), user("owner-b")
    bm = join(b)
    with pytest.raises(ValidationError):
        record(customer=a, member=bm, kind="closed", reference="wrong", source="admin")


def test_historical_backfill_preserves_times_and_is_idempotent(owner):
    User = get_user_model()
    past = timezone.now() - timedelta(days=200)
    User.objects.bulk_create(
        [User(username="legacy-history", email="legacy@example.invalid", date_joined=past)]
    )
    old = User.objects.get(username="legacy-history")
    migration = import_module("apps.loyalty.migrations.0006_guestevent")
    with transaction.atomic():
        # Only the connection alias is used by this pure historical data importer.
        from types import SimpleNamespace

        migration.history(apps, SimpleNamespace(connection=connection))
        before = GuestEvent.objects.count()
        migration.history(apps, SimpleNamespace(connection=connection))
    event = GuestEvent.objects.get(reference=f"account:{old.pk}")
    assert event.occurred_at == past and event.source == "history" and event.actor_id is None
    assert event.recorded_at > past
    assert GuestEvent.objects.count() == before


@pytest.mark.django_db(transaction=True)
def test_events_in_backup_and_immutable_comparison():
    join(user("backup"))
    data = capture(source_id="fictional-events")
    assert validate(data)["counts"]["loyalty.guestevent"] == 3
    changed = copy.deepcopy(data)
    next(r for r in changed["records"] if r["model"] == "loyalty.guestevent")["fields"][
        "reason"
    ] = "tamper"
    with pytest.raises(BackupError, match="تعذر"):
        compare(data, seal(changed))


@pytest.mark.django_db(transaction=True)
def test_previous_backup_schema_still_valid_without_forging_events():
    join(user("legacy-backup"))
    current = capture(source_id="fictional-legacy-events")
    old = copy.deepcopy(current)
    old["schema"] = _schema_hash(guest_events=False)
    old["records"] = [r for r in old["records"] if r["model"] != "loyalty.guestevent"]
    old = seal(old)
    assert "loyalty.guestevent" not in validate(old)["counts"]
    compare(old, current)
    bad = copy.deepcopy(current)
    bad["schema"] = old["schema"]
    with pytest.raises(BackupError):
        validate(seal(bad))
