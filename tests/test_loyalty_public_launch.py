"""Public registration and retained owner acceptance; no real identities or sends."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import VerifiedPhone
from apps.accounts.phone_copy import TEXT
from apps.loyalty.copy import copy
from apps.loyalty.launch import historical_award_allowed, stay_exception_allowed
from apps.loyalty.models import Ledger, Member, Referral
from apps.loyalty.services import balance, invitation_eligible, issue_invite, reconcile
from tests.test_loyalty_final_readiness import configure_stay_exception, future_stay
from tests.test_loyalty_stage8 import booking, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def public_programme(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_PILOT_ONLY = False
    settings.LOYALTY_PILOT_EMAILS = []
    settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "fictional-approved-v1"
    settings.LOYALTY_TERMS_REFERENCE = "/fictional-terms/"
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.LOYALTY_RETAIN_ACCEPTANCE_EXCEPTION = False
    settings.LOYALTY_MESSAGES_ENABLED = False
    settings.ACCOUNT_WHATSAPP_AUTH_ENABLED = True
    settings.ACCOUNT_WHATSAPP_AUTH_ISOLATED = True
    settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC = True
    settings.ACCOUNT_WHATSAPP_AUTH_EMAILS = []
    settings.ULTRAMSG_ENABLED = True
    settings.ULTRAMSG_INSTANCE_ID = "fictional"
    settings.ULTRAMSG_TOKEN = "fictional"
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 10, 13, tzinfo=UTC))


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_public_guest_without_booking_sees_verification_and_can_join_only_with_consent(
    settings, language
):
    guest = user("public-new")
    client = Client()
    client.force_login(guest)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    page = client.get("/my-bookings/rewards/")
    assert page.status_code == 200 and page.context["loyalty_mobile_verify"]
    assert 'href="/account/mobile/link/"' in page.content.decode()
    assert TEXT[language]["verify_mobile"] in page.content.decode()
    assert copy(language)["pilot_notice"] not in page.content.decode()
    assert not Member.objects.exists() and not Ledger.objects.exists()
    assert client.post("/my-bookings/rewards/", {"action": "join"}).status_code == 200
    assert not Member.objects.exists()
    assert (
        client.post("/my-bookings/rewards/", {"action": "join", "consent": "on"}).status_code == 302
    )
    member = Member.objects.get(customer=guest)
    assert member.terms_version == "fictional-approved-v1" and member.language == language
    assert balance(member) == 0 and not Ledger.objects.exists()
    assert not invitation_eligible(member)
    page = client.get("/my-bookings/rewards/")
    assert page.context["loyalty_mobile_verify"]
    VerifiedPhone.objects.create(user=guest, phone="+12025550123")
    assert not client.get("/my-bookings/rewards/").context["loyalty_mobile_verify"]


def test_public_join_still_requires_verified_email(settings):
    client = Client()
    client.force_login(user("not-verified", verified=False))
    assert not client.get("/my-bookings/rewards/").context["loyalty_mobile_verify"]
    assert (
        client.post("/my-bookings/rewards/", {"action": "join", "consent": "on"}).status_code == 403
    )
    assert not Member.objects.exists()


def test_public_launch_retains_only_an_already_posted_owner_exception(settings):
    guest = user("retained-owner")
    stay = future_stay(guest)
    configure_stay_exception(settings, stay)
    settings.LOYALTY_PILOT_ALLOW_HISTORICAL = True
    member = join(guest)
    token = issue_invite(guest, stay.pk)
    member.refresh_from_db()
    assert balance(member) == Decimal("22.50") and invitation_eligible(member)
    count = Ledger.objects.count()
    settings.LOYALTY_PILOT_ONLY = False
    assert not invitation_eligible(member)
    settings.LOYALTY_RETAIN_ACCEPTANCE_EXCEPTION = True
    stay.refresh_from_db()
    assert stay_exception_allowed(stay) and historical_award_allowed(stay)
    assert invitation_eligible(member)
    assert reconcile(stay.pk)["status"] == "unchanged"
    assert Ledger.objects.count() == count and balance(member) == Decimal("22.50")
    invitee = user("public-invitee")
    new_member = join(invitee, invitation_token=token, confirmed_inviter=member.pk)
    assert Referral.objects.get(member=new_member).inviter_id == member.pk
    assert balance(new_member) == 0
    other_stay = future_stay(invitee)
    assert not stay_exception_allowed(other_stay)
    assert reconcile(other_stay.pk)["status"] == "arrival_required"
    assert not invitation_eligible(new_member)
    stay.normalized_status = stay.Status.CANCELLED
    stay.save(update_fields=["normalized_status"])
    assert reconcile(stay.pk)["status"] == "settled"
    assert balance(member) == 0 and not invitation_eligible(member)


def test_retained_flag_cannot_grant_a_new_exception_or_follow_reassigned_booking(settings):
    guest = user("new-not-retained")
    stay = future_stay(guest)
    configure_stay_exception(settings, stay)
    settings.LOYALTY_PILOT_ONLY = False
    settings.LOYALTY_RETAIN_ACCEPTANCE_EXCEPTION = True
    assert not stay_exception_allowed(stay)
    member = join(guest)
    assert not Ledger.objects.exists() and balance(member) == 0
    settings.LOYALTY_PILOT_ONLY = True
    assert reconcile(stay.pk)["status"] == "settled"
    stay.refresh_from_db()
    other = user("different-owner")
    settings.LOYALTY_PILOT_EMAILS.append(other.email)
    stay.booking_intent.customer = other
    stay.booking_intent.save(update_fields=["customer"])
    settings.LOYALTY_PILOT_ONLY = False
    assert not stay_exception_allowed(stay)


def test_public_accrual_still_requires_cash_arrival_and_current_booking():
    guest = user("public-rules")
    member = join(guest)
    stay = booking(guest, arrived=False)
    assert reconcile(stay.pk)["status"] == "arrival_required"
    assert balance(member) == 0
    paid_stay = booking(guest)
    assert reconcile(paid_stay.pk)["status"] == "settled"
    assert balance(member) == Decimal("22.50")
