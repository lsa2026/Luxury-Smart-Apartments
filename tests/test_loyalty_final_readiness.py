"""Acceptance setup is bounded; all bookings and identities here are fictional."""

import json
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from io import StringIO
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.accounts.models import profile_for
from apps.accounts.tokens import make_verification_code
from apps.loyalty.launch import booking_allowed, historical_award_allowed, participant_allowed
from apps.loyalty.models import Ledger, Member
from apps.loyalty.notifications import messages_enabled
from apps.loyalty.services import balance, issue_invite, reconcile, restore_points, transfer
from tests.test_loyalty_stage8 import booking, completed_inviter, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def local_program(settings, monkeypatch):
    settings.LOYALTY_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    return clock


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links, self.inputs = [], {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "a":
            self.links.append(fields.get("href", ""))
        elif tag == "input":
            self.inputs[fields.get("name")] = fields.get("value", "")


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_new_invitee_keeps_inviter_through_login_register_and_verification(
    local_program, settings, language
):
    settings.LOYALTY_UI_ENABLED = settings.LOYALTY_UI_PREVIEW = settings.DEBUG = True
    # PostgreSQL must never accept draft consent through the SQLite preview gate.
    # Simulate a configured terms release for this fictional end-to-end test only.
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "test-fictional-terms-v1"
    settings.LOYALTY_TERMS_REFERENCE = "/test-only-loyalty-terms/"
    inviter, token = completed_inviter(user("referrer"), local_program)
    target = f"/my-bookings/rewards/?invite={token}"
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.get(target)
    assert response.status_code == 302
    response = client.get(response.url)
    register_url = next(
        x for x in Elements(response.content.decode()).links if x.startswith("/register/")
    )
    assert parse_qs(urlparse(register_url).query)["next"] == [target]
    response = client.get(register_url)
    html = Elements(response.content.decode())
    login_url = next(x for x in html.links if x.startswith("/login/"))
    assert parse_qs(urlparse(login_url).query)["next"] == [target]
    response = client.post(
        "/register/",
        {
            "first_name": "Example",
            "last_name": "Invited",
            "email": "invitee@example.invalid",
            "accept_terms": "on",
            "next": html.inputs["next"],
        },
    )
    assert response.url == "/account/confirm/"
    from django.contrib.auth import get_user_model

    invited = get_user_model().objects.get(email="invitee@example.invalid")
    profile = profile_for(invited)
    code = make_verification_code(invited.pk, invited.email, profile.verification_sent_at)
    assert client.post("/account/confirm/code/", {"code": code}).url == target
    assert not Member.objects.filter(customer=invited).exists()
    response = client.get(target)
    assert response.context["loyalty_inviter"].pk == inviter.pk
    client.post(target, {"action": "join", "consent": "on", "confirm_inviter": "on"})
    member = Member.objects.get(customer=invited)
    assert member.referral.inviter_id == inviter.pk
    assert balance(member) == 0


def test_pilot_is_closed_for_empty_or_invalid_scope(settings):
    settings.LOYALTY_PILOT_ONLY = True
    u = user("empty")
    assert not participant_allowed(u)
    settings.LOYALTY_PILOT_EMAILS = [u.email, "*"]
    assert not participant_allowed(u)
    with pytest.raises(PermissionDenied):
        join(u)
    assert not Member.objects.exists()


def test_historical_acceptance_is_exact_and_does_not_backdate_consent(local_program, settings):
    owner = user("historical")
    r = booking(owner)
    r.created_at = r.created_at - timedelta(days=30)
    r.save(update_fields=["created_at"])
    local_program[0] += timedelta(days=4)
    settings.LOYALTY_PILOT_ONLY = True
    settings.LOYALTY_PILOT_EMAILS = [owner.email]
    settings.LOYALTY_PILOT_BOOKING_REFERENCES = [r.public_reference]
    settings.LOYALTY_PILOT_ALLOW_HISTORICAL = True
    assert booking_allowed(r) and historical_award_allowed(r)
    member = join(owner)
    assert member.consent_at == local_program[0]
    assert balance(member) == pytest.approx(22.50)
    assert reconcile(r.pk)["status"] == "unchanged"
    assert Ledger.objects.filter(member=member).count() == 1
    original_dates = (r.check_in, r.check_out)
    issue_invite(owner, r.pk)
    r.refresh_from_db()
    assert (r.check_in, r.check_out) == original_dates
    assert (
        r.loyalty_state["settlement"]["facts"]["acceptance_scope"]
        == "explicit-historical-live-acceptance"
    )
    unrelated = booking(owner, "950.00")
    before_state = unrelated.loyalty_state.copy()
    assert reconcile(unrelated.pk)["status"] == "outside_pilot_scope"
    unrelated.refresh_from_db()
    assert unrelated.loyalty_state == before_state
    assert not historical_award_allowed(unrelated)
    assert not Ledger.objects.filter(reservation=unrelated).exists()


def test_historical_override_without_private_scope_has_no_effect(local_program, settings):
    u = user("ordinary")
    r = booking(u)
    r.created_at -= timedelta(days=30)
    r.save(update_fields=["created_at"])
    settings.LOYALTY_PILOT_ALLOW_HISTORICAL = True
    join(u)
    assert not historical_award_allowed(r)
    assert reconcile(r.pk)["status"] == "outside_program_scope"
    assert not Ledger.objects.exists()


def test_historical_invitation_is_offered_only_for_selected_completed_stay(local_program, settings):
    u = user("past-invitation")
    r = booking(u)
    unrelated = booking(u, "950.00")
    r.created_at -= timedelta(days=30)
    r.save(update_fields=["created_at"])
    local_program[0] += timedelta(days=4)
    settings.LOYALTY_UI_ENABLED = settings.LOYALTY_UI_PREVIEW = settings.DEBUG = True
    settings.LOYALTY_PILOT_ONLY = settings.LOYALTY_PILOT_ALLOW_HISTORICAL = True
    settings.LOYALTY_PILOT_EMAILS = [u.email]
    settings.LOYALTY_PILOT_BOOKING_REFERENCES = [r.public_reference]
    join(u)
    client = Client()
    client.force_login(u)
    stays = client.get("/my-bookings/rewards/").context["loyalty_completed"]
    assert [stay.pk for stay in stays] == [r.pk]
    assert unrelated.pk not in [stay.pk for stay in stays]


def test_out_of_scope_points_cannot_change_cash_due_or_restore_balance(local_program, settings):
    from apps.loyalty.checkout import cash_due, settle_booking_points

    u = user("unselected-checkout")
    member = join(u)
    r = booking(u)
    settings.LOYALTY_PILOT_ONLY = True
    settings.LOYALTY_PILOT_EMAILS = [u.email]
    settings.LOYALTY_PILOT_BOOKING_REFERENCES = ["TEST-ANOTHER-STAY"]
    assert cash_due(r.booking_intent) == r.booking_intent.payment_amount_sar
    r.loyalty_state["hold"] = {"member": member.pk, "value": "10.00"}
    r.save(update_fields=["loyalty_state"])
    original = r.loyalty_state.copy()
    with pytest.raises(ValidationError):
        cash_due(r.booking_intent, freeze=True)
    assert settle_booking_points(r.pk) is None
    with pytest.raises(PermissionDenied):
        restore_points(r.pk, "10.00", "TEST-REFUND")
    r.refresh_from_db()
    assert r.loyalty_state == original
    assert not Ledger.objects.exists()


def test_pilot_hides_interface_for_other_users_and_suppresses_automatic_messages(
    local_program, settings
):
    a, b = user("allowed"), user("other")
    settings.LOYALTY_UI_ENABLED = settings.LOYALTY_UI_PREVIEW = settings.DEBUG = True
    settings.LOYALTY_PILOT_ONLY = True
    settings.LOYALTY_PILOT_EMAILS = [a.email]
    settings.LOYALTY_MESSAGES_ENABLED = True
    c = Client()
    c.force_login(b)
    assert "loyalty_ui" not in c.get("/my-bookings/rewards/").context
    assert c.post("/my-bookings/rewards/", {"action": "join", "consent": "on"}).status_code == 405
    c.force_login(a)
    assert c.get("/my-bookings/rewards/").context["loyalty_ui"] is True
    assert not messages_enabled()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_live_pilot_banner_never_labels_real_points_as_fictional(settings, language):
    from apps.loyalty.copy import copy

    u = user("live-banner")
    settings.LOYALTY_UI_ENABLED = settings.LOYALTY_PILOT_ONLY = True
    settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_PILOT_EMAILS = [u.email]
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    client.force_login(u)
    response = client.get("/my-bookings/rewards/")
    assert response.context["loyalty_pilot"] is True
    assert response.context["loyalty_preview"] is False
    assert copy(language)["pilot_notice"] in response.content.decode()
    assert copy(language)["preview"] not in response.content.decode()


def test_pilot_transfer_to_outside_account_rolls_back_both_sides(local_program, settings):
    a, b = user("sender"), user("recipient")
    am, _ = completed_inviter(a, local_program)
    bm = join(b)
    count = Ledger.objects.count()
    settings.LOYALTY_PILOT_ONLY = True
    settings.LOYALTY_PILOT_EMAILS = [a.email]
    with pytest.raises(PermissionDenied):
        transfer(a, "TEST-PILOT-TRANSFER", bm.number, "10.00")
    assert Ledger.objects.count() == count
    assert balance(am) == pytest.approx(22.50)
    assert balance(bm) == 0


def test_readiness_command_performs_no_writes(local_program, settings):
    from django.db import connection

    u = user("read-only")
    r = booking(u)
    before = (Member.objects.count(), Ledger.objects.count(), r.loyalty_state.copy())
    output = StringIO()

    def select_only(execute, sql, params, many, context):
        assert sql.lstrip().upper().startswith("SELECT"), "Readiness must never write"
        return execute(sql, params, many, context)

    with connection.execute_wrapper(select_only):
        call_command("loyalty_readiness", reservation=r.public_reference, stdout=output)
    result = json.loads(output.getvalue())
    assert result["read_only"] is True
    assert result["gates"]["terms_reference_configured"] is False
    assert result["blocker"] == "membership_required"
    r.refresh_from_db()
    assert before == (Member.objects.count(), Ledger.objects.count(), r.loyalty_state)
    assert u.email not in output.getvalue()
    assert r.public_reference not in output.getvalue()


def test_readiness_accepts_admin_booking_id_while_launch_is_unconfigured(settings):
    u = user("not-launched")
    r = booking(u)
    settings.LOYALTY_ENABLED = False
    settings.LOYALTY_LAUNCH_AT = ""
    output = StringIO()
    call_command("loyalty_readiness", reservation_id=str(r.pk), stdout=output)
    result = json.loads(output.getvalue())
    assert result["booking_found"] is True
    assert result["linked_customer_recorded"] is True
    assert result["arrival_recorded"] is True
    assert result["gates"]["launch_date_configured"] is False
    assert result["award_evidence_ready"] is False
    assert result["blocker"] == "launch_date_required"
    assert str(r.pk) not in output.getvalue()
    assert not Member.objects.exists() and not Ledger.objects.exists()


def test_readiness_invalid_booking_identifier_fails_without_echoing_it():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="valid reservation identifier"):
        call_command("loyalty_readiness", reservation_id="not-a-booking")


@pytest.mark.parametrize("paid,arrived", [(False, True), (True, False)])
def test_historical_acceptance_still_requires_real_payment_and_arrival(
    local_program, settings, paid, arrived
):
    u = user("evidence")
    r = booking(u, paid=paid, arrived=arrived)
    r.created_at -= timedelta(days=30)
    r.save(update_fields=["created_at"])
    local_program[0] += timedelta(days=4)
    settings.LOYALTY_PILOT_ONLY = settings.LOYALTY_PILOT_ALLOW_HISTORICAL = True
    settings.LOYALTY_PILOT_EMAILS = [u.email]
    settings.LOYALTY_PILOT_BOOKING_REFERENCES = [r.public_reference]
    join(u)
    assert not Ledger.objects.exists()


def test_background_event_cannot_touch_an_unselected_booking(local_program, settings, monkeypatch):
    from apps.loyalty import checkout
    from apps.loyalty.site_adapter import _safe_reconcile

    u = user("background")
    r = booking(u)
    original = r.loyalty_state.copy()
    settings.LOYALTY_PILOT_ONLY = True
    settings.LOYALTY_PILOT_EMAILS = [u.email]
    settings.LOYALTY_PILOT_BOOKING_REFERENCES = ["TEST-SOME-OTHER-BOOKING"]

    def forbidden(*args, **kwargs):
        raise AssertionError("An unrelated booking must not reach settlement")

    monkeypatch.setattr(checkout, "settle_booking_points", forbidden)
    _safe_reconcile(r.pk)
    r.refresh_from_db()
    assert r.loyalty_state == original


@pytest.mark.parametrize(
    "target", ["https://outside.example.invalid/", "//outside.example.invalid/"]
)
def test_preserving_next_does_not_allow_external_redirects(target):
    from django.contrib.auth import get_user_model

    client = Client()
    client.post(
        "/register/",
        {
            "first_name": "Example",
            "last_name": "Guest",
            "email": "safe@example.invalid",
            "accept_terms": "on",
            "next": target,
        },
    )
    u = get_user_model().objects.get(email="safe@example.invalid")
    profile = profile_for(u)
    code = make_verification_code(u.pk, u.email, profile.verification_sent_at)
    assert client.post("/account/confirm/code/", {"code": code}).url == "/my-bookings/"
