"""Short invitations preserve existing attribution; clipboard UI is guest initiated."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.loyalty.copy import copy
from apps.loyalty.invitation_links import SHORT_LENGTH, invitation_url
from apps.loyalty.models import Ledger, Member, Referral
from tests.test_loyalty_stage8 import completed_inviter, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def programme(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "fictional-short-invite-terms"
    settings.LOYALTY_TERMS_REFERENCE = "/fictional-short-invite-terms/"
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    return clock


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_short_invite_and_copy_button_are_localized_and_keep_original_token(programme, language):
    inviter, token = completed_inviter(user("short-owner"), programme)
    client = Client()
    client.force_login(inviter.customer)
    client.cookies["django_language"] = language
    page = client.get(reverse("accounts:rewards"))
    short = page.context["loyalty_invite_url"]
    assert urlparse(short).path == f"/i/{token[:SHORT_LENGTH]}/"
    assert urlparse(short).netloc == "testserver"
    assert len(short) < len("http://testserver/my-bookings/rewards/?invite=" + token)
    html = page.content.decode()
    assert copy(language)["copy_invite"] in html
    assert copy(language)["copy_success"] in html
    assert "data-invite-copy-button" in html and 'type="button"' in html
    assert 'role="status"' in html and 'aria-live="polite"' in html
    assert "loyalty-invite.js" in html
    inviter.refresh_from_db()
    assert inviter.invitation_token == token


def test_short_link_preserves_full_invitation_through_registration_and_attribution(programme):
    inviter, token = completed_inviter(user("inviter"), programme)
    member_count, movement_count = Member.objects.count(), Ledger.objects.count()
    client = Client()
    short_path = reverse("loyalty_invite_short", kwargs={"code": token[:SHORT_LENGTH]})
    response = client.get(short_path + "?next=https://outside.example.invalid/")
    legacy = reverse("accounts:rewards") + "?invite=" + token
    assert response.status_code == 302 and response.url == legacy
    assert "no-store" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "no-referrer"
    assert "noindex" in response["X-Robots-Tag"]
    assert Member.objects.count() == member_count and Ledger.objects.count() == movement_count
    login_response = client.get(response.url)
    assert parse_qs(urlparse(login_response.url).query)["next"] == [legacy]
    assert client.get(legacy).url == login_response.url  # Old links remain valid.
    from django.contrib.auth import get_user_model

    from apps.accounts.models import profile_for
    from apps.accounts.tokens import make_verification_code

    client.post(
        "/register/",
        {
            "first_name": "Fictional",
            "last_name": "Invitee",
            "email": "short-invitee@example.invalid",
            "accept_terms": "on",
            "next": legacy,
        },
    )
    invited = get_user_model().objects.get(email="short-invitee@example.invalid")
    profile = profile_for(invited)
    code = make_verification_code(invited.pk, invited.email, profile.verification_sent_at)
    assert client.post("/account/confirm/code/", {"code": code}).url == legacy
    page = client.get(legacy)
    assert page.context["loyalty_inviter"].pk == inviter.pk
    assert not Member.objects.filter(customer=invited).exists()
    assert client.post(legacy, {"action": "join", "consent": "on"}).status_code == 200
    assert not Member.objects.filter(customer=invited).exists()
    assert (
        client.post(
            legacy, {"action": "join", "consent": "on", "confirm_inviter": "on"}
        ).status_code
        == 302
    )
    assert Referral.objects.get(member__customer=invited).inviter_id == inviter.pk
    assert Ledger.objects.count() == movement_count  # Invitation itself earns nothing.


def test_short_prefix_collision_falls_back_without_attributing_to_wrong_member(programme):
    from django.test import RequestFactory

    first, token = completed_inviter(user("first"), programme)
    second, other = completed_inviter(user("second"), programme)
    second.invitation_token = token[:SHORT_LENGTH] + other[SHORT_LENGTH:]
    second.save(update_fields=["invitation_token"])
    first.refresh_from_db()
    request = RequestFactory().get("/my-bookings/rewards/")
    short_path = reverse("loyalty_invite_short", kwargs={"code": token[:SHORT_LENGTH]})
    client = Client()
    assert client.get(short_path).status_code == 404
    full_path = urlparse(invitation_url(request, first)).path
    assert full_path == f"/i/{token}/"
    response = client.get(full_path)
    assert parse_qs(urlparse(response.url).query)["invite"] == [token]


@pytest.mark.parametrize("disabled", ["member", "customer", "programme", "interface", "pilot"])
def test_short_alias_respects_active_membership_programme_and_private_scope(
    programme, settings, disabled
):
    member, token = completed_inviter(user("limited"), programme)
    if disabled == "member":
        member.status = "withdrawn"
        member.save(update_fields=["status"])
    elif disabled == "customer":
        member.customer.is_active = False
        member.customer.save(update_fields=["is_active"])
    elif disabled == "programme":
        settings.LOYALTY_ENABLED = False
    elif disabled == "interface":
        settings.LOYALTY_UI_ENABLED = False
    else:
        settings.LOYALTY_PILOT_ONLY = True
        settings.LOYALTY_PILOT_EMAILS = ["another@example.invalid"]
    path = reverse("loyalty_invite_short", kwargs={"code": token[:SHORT_LENGTH]})
    assert Client().get(path).status_code == 404


def test_unearned_invitation_and_invalid_or_unsafe_requests_are_rejected():
    member = join(user("not-completed"))
    member.invitation_token = "Z" * 43
    member.save(update_fields=["invitation_token"])
    client = Client()
    for code in ["short", "Z" * 16, "X" * 101]:
        assert client.get(reverse("loyalty_invite_short", kwargs={"code": code})).status_code == 404
    path = reverse("loyalty_invite_short", kwargs={"code": "Z" * 16})
    assert client.post(path).status_code == 405
    assert Ledger.objects.count() == 0


def test_full_alias_rechecks_selected_trial_eligibility_after_cancellation(settings):
    from apps.loyalty.services import issue_invite, reconcile
    from apps.reservations.models import Reservation
    from tests.test_loyalty_final_readiness import configure_stay_exception, future_stay

    guest = user("cancelled-trial")
    booking = future_stay(guest)
    configure_stay_exception(settings, booking)
    join(guest)
    token = issue_invite(guest, booking.pk)
    booking.normalized_status = Reservation.Status.CANCELLED
    booking.save(update_fields=["normalized_status"])
    reconcile(booking.pk)
    assert Client().get(reverse("loyalty_invite_short", kwargs={"code": token})).status_code == 404
