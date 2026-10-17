"""Fictional accounts and offline WhatsApp provider; no production credentials."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import Client

from apps.accounts.identity import account_verified, update_identity
from apps.accounts.models import ApprovedPhoneChange, PhoneChallenge, VerifiedPhone, profile_for
from apps.loyalty.accrual import IDENTITY_TERMS_REFERENCE, IDENTITY_TERMS_VERSION, policy_enabled
from apps.loyalty.identity_ui import snapshot
from apps.loyalty.models import Ledger
from apps.loyalty.public_terms import document
from apps.loyalty.services import balance
from apps.notifications.models import AuditLog
from tests import test_guest_phone_auth as phone_fixtures
from tests.test_guest_phone_auth import CODE, PHONE, deliver, start
from tests.test_hyperpay import own_intent, payable_intent
from tests.test_loyalty_stage8 import booking, join, reconcile, user

pytestmark = pytest.mark.django_db
guest = phone_fixtures.guest
isolated = phone_fixtures.isolated
NEW_PHONE = "+12125551234"


@pytest.fixture(autouse=True)
def programme(settings, isolated):
    settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC = True
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_PILOT_ONLY = settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = IDENTITY_TERMS_VERSION
    settings.LOYALTY_TERMS_REFERENCE = IDENTITY_TERMS_REFERENCE
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = False
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = False
    isolated.clock[0] = isolated.clock[0].replace(hour=13)


@pytest.fixture
def administrator():
    u = get_user_model().objects.create_superuser(username="fictional-identity-owner")
    c = Client()
    c.force_login(u)
    return c


def member_with_phone(guest):
    member = join(guest.user)
    p = profile_for(guest.user)
    p.phone = PHONE
    p.save(update_fields=["phone"])
    VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    return member


def change(administrator, member, *, phone=NEW_PHONE, email=None, expected=None):
    page = administrator.get("/admin/loyalty/identity/", {"member": member.pk})
    assert page.status_code == 200
    token = expected or page.context["form"].initial["snapshot"]
    return administrator.post(
        "/admin/loyalty/identity/",
        {
            "member": str(member.pk),
            "phone_country": "US",
            "phone": phone,
            "email": email or member.customer.email,
            "reason": "طلب الضيف بعد التحقق من هويته",
            "verified": "on",
            "snapshot": token,
        },
    )


def test_admin_changes_do_not_move_membership_balance_or_inviter(
    guest, administrator, isolated, django_capture_on_commit_callbacks
):
    member = member_with_phone(guest)
    reconcile(booking(guest.user).pk)
    before = (member.pk, member.number, balance(member), member.referral.tree_number)
    original = list(Ledger.objects.values("pk", "credit", "debit", "evidence"))
    assert change(administrator, member, email="new-mail@example.invalid").status_code == 302
    member.refresh_from_db()
    guest.user.refresh_from_db()
    assert guest.user.email == "new-mail@example.invalid"
    assert not profile_for(guest.user).is_email_verified
    assert not VerifiedPhone.objects.filter(user=guest.user).exists()
    assert ApprovedPhoneChange.objects.get(user=guest.user).phone == NEW_PHONE
    assert before == (member.pk, member.number, balance(member), member.referral.tree_number)
    assert original == list(Ledger.objects.values("pk", "credit", "debit", "evidence"))
    assert not isolated.sent and not isolated.dispatched
    audit = AuditLog.objects.get(action="guest.identity_changed")
    assert str(audit.actor_user_id) == administrator.session["_auth_user_id"]
    assert audit.metadata["old_phone"] == PHONE and audit.metadata["new_phone"] == NEW_PHONE
    assert audit.metadata["reason"]
    # Old sessions end; the new mobile is NOT a credential until its owner proves it.
    assert guest.client.get("/my-bookings/rewards/").url == "/account/mobile/"
    c = Client()
    assert start(c, django_capture_on_commit_callbacks, phone=NEW_PHONE).status_code == 302
    assert not VerifiedPhone.objects.filter(user=guest.user).exists()
    row = deliver(c)
    assert row.status == "sent"
    assert c.post("/account/mobile/confirm/", {"code": CODE}).status_code == 302
    assert VerifiedPhone.objects.get(user=guest.user).phone == NEW_PHONE
    assert not ApprovedPhoneChange.objects.filter(user=guest.user).exists()
    assert c.get("/my-bookings/rewards/").status_code == 200
    assert account_verified(guest.user) and not profile_for(guest.user).is_email_verified


def test_email_only_change_keeps_mobile_signin_and_never_verifies_new_email(
    guest, administrator, django_capture_on_commit_callbacks
):
    member = member_with_phone(guest)
    binding = VerifiedPhone.objects.get(user=guest.user)
    assert (
        change(administrator, member, phone=PHONE, email="new-mail@example.invalid").status_code
        == 302
    )
    guest.user.refresh_from_db()
    assert VerifiedPhone.objects.get(user=guest.user).version == binding.version
    assert not profile_for(guest.user).is_email_verified
    c = Client()
    start(c, django_capture_on_commit_callbacks)
    deliver(c)
    assert c.post("/account/mobile/confirm/", {"code": CODE}).status_code == 302
    assert c.get("/my-bookings/rewards/").status_code == 200


@pytest.mark.parametrize("path", ["/my-bookings/details/", "/my-bookings/"])
def test_member_cannot_change_or_remove_saved_phone_by_forged_profile_post(guest, path):
    member_with_phone(guest)
    guest.client.post(
        path, {"phone": NEW_PHONE, "phone_country": "US", "email": "forged@example.invalid"}
    )
    guest.user.refresh_from_db()
    assert profile_for(guest.user).phone == PHONE
    assert guest.user.email != "forged@example.invalid"
    assert VerifiedPhone.objects.get(user=guest.user).phone == PHONE
    assert not AuditLog.objects.filter(action="guest.identity_changed").exists()


def test_member_can_save_and_verify_first_mobile_but_not_rewrite_it(
    guest, django_capture_on_commit_callbacks
):
    join(guest.user)
    guest.client.post("/my-bookings/details/", {"phone": PHONE, "phone_country": "SA"})
    assert profile_for(guest.user).phone == PHONE
    assert (
        start(
            guest.client, django_capture_on_commit_callbacks, binding=True, phone=NEW_PHONE
        ).status_code
        == 200
    )
    assert not PhoneChallenge.objects.exists()
    assert start(guest.client, django_capture_on_commit_callbacks, binding=True).status_code == 302
    deliver(guest.client)
    assert guest.client.post("/account/mobile/confirm/", {"code": CODE}).status_code == 302
    assert VerifiedPhone.objects.get(user=guest.user).phone == PHONE


def test_alternate_allauth_email_change_is_forbidden(guest):
    member_with_phone(guest)
    response = guest.client.post(
        "/accounts/email/", {"action_add": "", "email": "forged@example.invalid"}
    )
    assert response.status_code == 403
    guest.user.refresh_from_db()
    assert guest.user.email != "forged@example.invalid"


@pytest.mark.parametrize("actor", ["guest", "staff", "anonymous"])
def test_admin_identity_page_cannot_be_used_by_other_roles(guest, actor):
    member_with_phone(guest)
    c = guest.client if actor != "anonymous" else Client()
    if actor == "staff":
        guest.user.is_staff = True
        guest.user.save(update_fields=["is_staff"])
    response = c.get("/admin/loyalty/identity/")
    assert response.status_code in {302, 403}
    assert not AuditLog.objects.filter(action="guest.identity_changed").exists()


def test_conflicting_mobile_email_and_stale_review_are_rejected(guest, administrator):
    member = member_with_phone(guest)
    other = user("identity-other")
    VerifiedPhone.objects.create(user=other, phone=NEW_PHONE)
    assert "مرتبط" in change(administrator, member).content.decode()
    assert "مرتبط" in change(administrator, member, phone=PHONE, email=other.email).content.decode()
    page = administrator.get("/admin/loyalty/identity/", {"member": member.pk})
    stale = page.context["form"].initial["snapshot"]
    VerifiedPhone.objects.filter(user=other).delete()
    assert change(administrator, member).status_code == 302
    assert "تغيرت" in change(administrator, member, phone=PHONE, expected=stale).content.decode()
    assert AuditLog.objects.filter(action="guest.identity_changed").count() == 1


def test_old_queued_phone_challenge_cannot_survive_admin_change(
    guest, administrator, django_capture_on_commit_callbacks
):
    member = member_with_phone(guest)
    c = Client()
    start(c, django_capture_on_commit_callbacks)
    row = deliver(c)
    assert row.status == "sent"
    change(administrator, member)
    row.refresh_from_db()
    assert row.status == "superseded" and not row.code_digest
    c.post("/account/mobile/confirm/", {"code": CODE})
    assert "_auth_user_id" not in c.session


def test_phone_primary_checkout_links_without_access_to_old_email(
    guest, django_capture_on_commit_callbacks
):
    member_with_phone(guest)
    p = profile_for(guest.user)
    p.email_verified_at = None
    p.save(update_fields=["email_verified_at"])
    intent = payable_intent()
    intent.customer = None
    intent.guest_phone, intent.guest_email = PHONE, "forgotten-mail@example.invalid"
    intent.save()
    c = Client()
    own_intent(c, intent)
    from django.urls import reverse

    action = reverse("reservations:booking_account", args=[intent.public_reference])
    login = c.post(action)
    assert login.url.startswith("/account/mobile/?next=")
    assert c.get(login.url).context["phone_form"].initial["phone"] == PHONE
    start(c, django_capture_on_commit_callbacks, target=action)
    deliver(c)
    destination = c.post("/account/mobile/confirm/", {"code": CODE}).url
    c.get(destination)
    intent.refresh_from_db()
    assert intent.customer_id == guest.user.pk
    assert intent.guest_email == "forgotten-mail@example.invalid"
    assert not Ledger.objects.exists()


def test_matching_email_cannot_override_somebody_elses_verified_mobile(guest):
    from apps.accounts.identity import booking_identity_matches

    member_with_phone(guest)
    other = user("different-primary")
    VerifiedPhone.objects.create(user=other, phone=NEW_PHONE)
    assert not booking_identity_matches(guest.user, guest.user.email, NEW_PHONE)
    assert booking_identity_matches(guest.user, "old-mail@example.invalid", PHONE)


def test_paid_return_after_a_year_without_signin_keeps_inviter_and_awards_once(guest, isolated):
    from apps.loyalty.tasks import accrue_due_stays_task
    from tests.test_loyalty_stage8 import completed_inviter

    inviter = user("returning-inviter")
    parent, token = completed_inviter(inviter, isolated.clock)
    member = join(guest.user, invitation_token=token, confirmed_inviter=parent.pk)
    VerifiedPhone.objects.create(user=guest.user, phone=PHONE)
    first = booking(guest.user)
    reconcile(first.pk)
    before = [balance(m) for m in [member, parent]]
    old_tree = member.referral.tree_number
    isolated.clock[0] += timedelta(days=365)
    r = booking(guest.user, "950.00", arrived=False)
    r.booking_intent.customer = None
    r.booking_intent.guest_phone = PHONE
    r.booking_intent.guest_email = "inaccessible@example.invalid"
    r.booking_intent.payment_amount_sar = Decimal("950.00")
    r.booking_intent.save()
    assert accrue_due_stays_task()["movements"] == 2
    assert accrue_due_stays_task()["movements"] == 0
    r.booking_intent.refresh_from_db()
    assert r.booking_intent.customer_id == guest.user.pk
    assert [balance(m) for m in [member, parent]] == [
        before[0] + Decimal("28.50"),
        before[1] + Decimal("47.50"),
    ]
    member.referral.refresh_from_db()
    assert member.referral.tree_number == old_tree
    # Matching a recipient never signs the booking browser into their account.
    assert "_auth_user_id" not in Client().session


@pytest.mark.parametrize(
    "blocker", ["unpaid", "unverified", "external", "prelaunch", "other_owner"]
)
def test_automatic_phone_match_never_bypasses_payment_proof_source_or_owner(guest, blocker):
    from apps.accounts.identity import attach_paid_phone_account
    from apps.loyalty.services import launch_at
    from apps.payments.models import PaymentAttempt
    from apps.reservations.models import Reservation

    member_with_phone(guest)
    r = booking(guest.user)
    r.booking_intent.customer = None
    r.booking_intent.guest_phone = PHONE
    r.booking_intent.payment_amount_sar = r.total_price
    r.booking_intent.save()
    if blocker == "unpaid":
        PaymentAttempt.objects.filter(booking_intent=r.booking_intent).delete()
    elif blocker == "unverified":
        VerifiedPhone.objects.filter(user=guest.user).delete()
    elif blocker == "external":
        r.source_type = Reservation.SourceType.EXTERNAL_CHANNEL
    elif blocker == "prelaunch":
        r.created_at = launch_at() - timedelta(days=1)
    else:
        r.booking_intent.customer = user("already-owned")
        r.booking_intent.save()
    owner_before = r.booking_intent.customer_id
    assert not attach_paid_phone_account(r)
    r.booking_intent.refresh_from_db()
    assert r.booking_intent.customer_id == owner_before


def test_admin_pending_phone_cannot_be_stolen_by_a_first_binding(guest, administrator):
    member = member_with_phone(guest)
    change(administrator, member)
    from apps.accounts.identity import can_bind_phone

    assert not can_bind_phone(user("other-first-binding"), NEW_PHONE)


def test_email_only_admin_change_before_first_mobile_still_allows_first_verification(
    guest, administrator
):
    member = join(guest.user)
    assert (
        change(administrator, member, phone="", email="replacement@example.invalid").status_code
        == 302
    )
    assert not ApprovedPhoneChange.objects.filter(user=guest.user).exists()
    assert not VerifiedPhone.objects.filter(user=guest.user).exists()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_third_terms_edition_retains_both_prior_editions_and_timed_policy(language):
    c = Client()
    for version in ["2026-10-16-v1", "2026-10-16-v2", "2026-10-17-v3"]:
        assert c.get(f"/{language}/legal/rewards/{version}/").status_code == 200
    assert document(language, version=IDENTITY_TERMS_VERSION)["version"] == IDENTITY_TERMS_VERSION
    assert policy_enabled()


@pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires isolated PostgreSQL")
@pytest.mark.django_db(transaction=True)
def test_two_admin_changes_cannot_reserve_one_mobile_for_two_accounts(administrator):
    from django.test import RequestFactory

    a, b = user("conflict-a"), user("conflict-b")
    am, bm = join(a), join(b)
    operator = get_user_model().objects.get(pk=administrator.session["_auth_user_id"])
    expected = {a.pk: snapshot(am), b.pk: snapshot(bm)}

    def execute(guest_user):
        close_old_connections()
        try:
            request = RequestFactory().post("/")
            request.user = operator
            try:
                update_identity(
                    request,
                    guest_user.pk,
                    email=guest_user.email,
                    phone=NEW_PHONE,
                    reason="اختبار التعارض بين طلبين إداريين",
                    expected=expected[guest_user.pk],
                )
                return True
            except ValidationError:
                return False
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(execute, [a, b])) == [False, True]
    assert ApprovedPhoneChange.objects.filter(phone=NEW_PHONE).count() == 1
