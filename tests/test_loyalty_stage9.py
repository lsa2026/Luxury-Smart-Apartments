"""Stage-nine guest/owner web journeys, isolated accounts and cash receipts only."""

from decimal import Decimal

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.loyalty.models import Ledger, Member
from apps.loyalty.services import balance, reconcile
from tests.test_loyalty_stage8 import booking, completed_inviter, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def ui(settings, monkeypatch):
    from datetime import UTC, datetime

    settings.LOYALTY_ENABLED = True
    settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_UI_PREVIEW = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.DEBUG = True
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    cache.clear()
    return clock


def login(u):
    c = Client()
    c.force_login(u)
    return c


def owner():
    u = user("owner")
    u.is_staff = u.is_superuser = True
    u.save()
    return u


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_localized_join_private_and_explicit(language, settings):
    from apps.loyalty.copy import copy

    u = user(language)
    c = login(u)
    c.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = c.get("/my-bookings/rewards/")
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert b"noindex,nofollow" in response.content
    assert not Member.objects.exists()
    assert response.context["loyalty_copy"] == copy(language)
    assert c.post("/my-bookings/rewards/", {"action": "join"}).status_code == 200
    assert not Member.objects.exists()
    assert (
        c.post(
            "/my-bookings/rewards/", {"action": "join", "consent": "on", "terms_version": "forged"}
        ).status_code
        == 302
    )
    m = Member.objects.get(customer=u)
    assert m.language == language
    assert m.terms_version == "local-preview-not-legal-v1"
    assert m.terms_version != "forged"


@pytest.mark.parametrize("flag", ["LOYALTY_ENABLED", "LOYALTY_UI_ENABLED"])
def test_closed_ui_keeps_original_placeholder_and_rejects_posts(flag, settings):
    setattr(settings, flag, False)
    c = login(user(flag))
    response = c.get("/my-bookings/rewards/")
    assert "loyalty_ui" not in response.context
    assert "loyalty.css" not in response.content.decode()
    assert c.post("/my-bookings/rewards/", {"action": "join", "consent": "on"}).status_code == 405
    assert not Member.objects.exists()
    assert login(owner()).get("/admin/loyalty/").status_code == 404


def test_unverified_cannot_join():
    u = user("unverified", verified=False)
    c = login(u)
    assert c.post("/my-bookings/rewards/", {"action": "join", "consent": "on"}).status_code == 403
    assert not Member.objects.exists()


def test_debug_flag_alone_cannot_enable_production_draft(settings):
    settings.DEBUG = False
    c = login(user("production"))
    assert c.post("/my-bookings/rewards/", {"action": "join", "consent": "on"}).status_code == 403
    assert not Member.objects.exists()


@pytest.mark.parametrize("reference", ["", "javascript:alert(1)", "//other.test/terms"])
def test_invalid_terms_reference_remains_readonly(reference, settings):
    settings.DEBUG = False
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "approved-v1"
    settings.LOYALTY_TERMS_REFERENCE = reference
    c = login(user(reference or "empty"))
    assert c.post("/my-bookings/rewards/", {"action": "join", "consent": "on"}).status_code == 403


def test_inviter_is_shown_and_must_be_explicitly_confirmed(ui):
    inviter, token = completed_inviter(user("a"), ui)
    guest = user("b")
    c = login(guest)
    url = f"/my-bookings/rewards/?invite={token}"
    response = c.get(url)
    assert response.context["loyalty_inviter"].pk == inviter.pk
    assert c.post(url, {"action": "join", "consent": "on"}).status_code == 200
    assert not Member.objects.filter(customer=guest).exists()
    assert (
        c.post(url, {"action": "join", "consent": "on", "confirm_inviter": "on"}).status_code == 302
    )
    assert Member.objects.get(customer=guest).referral.inviter_id == inviter.pk


def test_invalid_invite_cannot_silently_join_as_root():
    c = login(user("a"))
    assert (
        c.post(
            "/my-bookings/rewards/?invite=unknown", {"action": "join", "consent": "on"}
        ).status_code
        == 200
    )
    assert not Member.objects.exists()


def funded_pair():
    a, b = user("a"), user("b")
    am, bm = join(a), join(b)
    r = booking(a)
    reconcile(r.pk)
    return a, b, am, bm


def confirmation(c, bm, value="10.00"):
    response = c.post(
        "/my-bookings/rewards/",
        {"action": "transfer_review", "recipient": bm.number, "amount": value},
    )
    assert response.status_code == 200
    return response.context["loyalty_confirmation"]["token"]


def test_transfer_two_rows_atomically_and_replay_same_web_token_once():
    a, b, am, bm = funded_pair()
    c = login(a)
    token = confirmation(c, bm)
    assert balance(am) == Decimal("22.50")
    payload = {
        "action": "transfer_confirm",
        "confirmation": token,
        "amount": "1000.00",
        "recipient": am.number,
    }
    for _ in range(2):
        assert c.post("/my-bookings/rewards/", payload).status_code == 302
    assert balance(am) == Decimal("12.50")
    assert balance(bm) == Decimal("10.00")
    assert Ledger.objects.filter(event_type="transfer").count() == 2


def test_another_account_cannot_replay_a_transfer_confirmation():
    a, b, am, bm = funded_pair()
    token = confirmation(login(a), bm)
    assert (
        login(b)
        .post("/my-bookings/rewards/", {"action": "transfer_confirm", "confirmation": token})
        .status_code
        == 403
    )
    assert balance(am) == Decimal("22.50")
    assert balance(bm) == Decimal("0.00")


@pytest.mark.parametrize("value", ["0", "-1", "10.001", "NaN", "1000"])
def test_bad_transfer_amount_has_no_ledger_effect(value):
    a, b, am, bm = funded_pair()
    response = login(a).post(
        "/my-bookings/rewards/",
        {"action": "transfer_review", "recipient": bm.number, "amount": value},
    )
    assert response.context["loyalty_confirmation"] is None
    assert Ledger.objects.count() == 1


def test_expired_transfer_and_changed_signature_rejected(ui):
    from datetime import timedelta

    a, b, am, bm = funded_pair()
    c = login(a)
    token = confirmation(c, bm)
    for t in [token + "x"]:
        assert (
            c.post(
                "/my-bookings/rewards/", {"action": "transfer_confirm", "confirmation": t}
            ).status_code
            == 200
        )
    ui[0] += timedelta(minutes=11)
    # TimestampSigner uses wall time independently of the business test clock.
    import time
    from unittest.mock import patch

    with patch("django.core.signing.time.time", return_value=time.time() + 700):
        assert (
            c.post(
                "/my-bookings/rewards/", {"action": "transfer_confirm", "confirmation": token}
            ).status_code
            == 200
        )
    assert Ledger.objects.count() == 1


def test_csrf_is_required_for_all_guest_mutations():
    u = user("a")
    c = Client(enforce_csrf_checks=True)
    c.force_login(u)
    assert c.post("/my-bookings/rewards/", {"action": "join", "consent": "on"}).status_code == 403


def test_guest_statement_hides_other_guests_bookings_and_program_balance(ui):
    a = user("a")
    am, token = completed_inviter(a, ui)
    b = user("b")
    join(b, invitation_token=token, confirmed_inviter=am.pk)
    r = booking(b, reference="PRIVATE-BOOKING-B")
    reconcile(r.pk)
    response = login(a).get("/my-bookings/rewards/?member=99999")
    content = response.content.decode()
    assert "PRIVATE-BOOKING-B" not in content
    assert "رصيد البرنامج بعد العملية" not in content
    assert all(row.member_id == am.pk for row in response.context["loyalty_rows"])


def test_keyset_pagination_has_no_overlap_and_personal_running_balance():
    u = user("a")
    join(u)
    for n in range(22):
        reconcile(booking(u, reference=f"PAGE-{n}").pk)
    c = login(u)
    first = c.get("/my-bookings/rewards/")
    assert len(first.context["loyalty_rows"]) == 20
    cursor = first.context["loyalty_cursor"]
    second = c.get(f"/my-bookings/rewards/?before={cursor}")
    assert len(second.context["loyalty_rows"]) == 2
    assert not {r.pk for r in first.context["loyalty_rows"]} & {
        r.pk for r in second.context["loyalty_rows"]
    }
    assert first.context["loyalty_metrics"]["balance"] == Decimal("495.00")
    assert first.context["loyalty_rows"][0].member_balance_after == Decimal("495.00")


def test_invitation_get_does_not_issue_token_and_no_early_invite():
    u = user("a")
    m = join(u)
    r = booking(u)
    c = login(u)
    c.get("/my-bookings/rewards/")
    m.refresh_from_db()
    assert m.invitation_token is None
    assert (
        c.post("/my-bookings/rewards/", {"action": "invite", "reservation": str(r.pk)}).status_code
        == 200
    )
    m.refresh_from_db()
    assert m.invitation_token is None


@pytest.mark.parametrize("path", ["/admin/loyalty/", "/admin/loyalty/tree/"])
def test_only_operations_owner_can_see_ledger_and_tree(path):
    c = login(user("guest"))
    assert c.get(path).status_code == 302
    staff = user("staff")
    staff.is_staff = True
    staff.save()
    assert login(staff).get(path).status_code == 403
    assert login(owner()).get(path).status_code == 200


def test_tree_contains_zero_balance_members_and_automatic_relationships(ui):
    am, token = completed_inviter(user("a"), ui)
    bm = join(user("b"), invitation_token=token, confirmed_inviter=am.pk)
    c = login(owner())
    response = c.get(f"/admin/loyalty/tree/?parent={am.pk}")
    assert [r.member_id for r in response.context["tree_children"]] == [bm.pk]
    assert balance(bm) == 0
    assert c.get(f"/admin/loyalty/tree/?q={bm.number}").context["focus"].member_id == bm.pk
    assert c.get("/admin/loyalty/tree/?parent=invalid").status_code == 404


def test_admin_shows_program_balance_and_search_without_edit_routes():
    a, b, am, bm = funded_pair()
    c = login(owner())
    response = c.get(f"/admin/loyalty/?q={am.number}")
    assert response.status_code == 200
    assert response.context["program_balance"] == Decimal("22.50")
    assert response.context["loyalty_copy"]["program_total"] in response.content.decode()
    assert response.context["loyalty_copy"]["bookings_admin"] in response.content.decode()
    assert response.context["loyalty_copy"]["member_after"] in response.content.decode()
    assert len(response.context["loyalty_rows"]) == 1
    assert c.post("/admin/loyalty/", {"credit": "9999"}).status_code == 405


def test_admin_arrival_records_only_actual_evidence_and_auto_awards():
    u = user("a")
    am = join(u)
    r = booking(u, arrived=False)
    c = login(owner())
    response = c.post(
        "/admin/loyalty/arrival/",
        {
            "booking": r.public_reference,
            "arrived_at": "2026-10-10T15:05",
            "reference": "TEST-RECEPTION",
        },
    )
    assert response.status_code == 302
    r.refresh_from_db()
    assert r.loyalty_state["arrival"]["reference"] == "TEST-RECEPTION"
    assert balance(am) == Decimal("22.50")


def test_preview_rejects_arrival_on_non_test_reservation():
    u = user("a")
    join(u)
    r = booking(u, arrived=False)
    r.is_test = False
    r.hostaway_reservation_id = 900001
    r.save()
    assert (
        login(owner())
        .post(
            "/admin/loyalty/arrival/",
            {"booking": r.public_reference, "arrived_at": "2026-10-10T15:05", "reference": "X"},
        )
        .status_code
        == 403
    )
    r.refresh_from_db()
    assert not r.loyalty_state.get("arrival")


def test_saved_ratio_is_a_percentage_not_multiplied_by_one_hundred():
    from types import SimpleNamespace

    from apps.loyalty.views import _decorate

    rows = [
        SimpleNamespace(
            event_type="earn",
            operation_type="loyalty",
            allocation_numerator=3,
            allocation_denominator=1,
            rate_percent=Decimal("3.00"),
        ),
        SimpleNamespace(
            event_type="earn",
            operation_type="indirect_referral",
            allocation_numerator=7,
            allocation_denominator=8,
            rate_percent=Decimal("1.00"),
        ),
    ]
    _decorate(rows)
    assert rows[0].ui_rate == Decimal("3")
    assert rows[1].ui_rate == Decimal("0.875")


def test_feature_off_does_not_read_loyalty_tables(settings, django_assert_num_queries):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    settings.LOYALTY_UI_ENABLED = False
    c = login(user("off"))
    with CaptureQueriesContext(connection) as queries:
        response = c.get("/my-bookings/rewards/")
    assert response.status_code == 200
    assert not any("loyalty_" in q["sql"].lower() for q in queries)


@pytest.mark.parametrize("cursor", ["invalid", "-1", "99999999999999999999999999999"])
def test_malformed_ledger_cursor_does_not_crash(cursor):
    a, b, am, bm = funded_pair()
    assert login(a).get(f"/my-bookings/rewards/?before={cursor}").status_code == 200
    c = login(owner())
    assert c.get(f"/admin/loyalty/?before={cursor}").status_code == 200
    assert c.get(f"/admin/loyalty/tree/?after={cursor}").status_code == 200


def test_invalid_reservation_id_for_invite_is_a_normal_rejected_request():
    u = user("a")
    join(u)
    assert (
        login(u)
        .post("/my-bookings/rewards/", {"action": "invite", "reservation": "not-a-uuid"})
        .status_code
        == 200
    )
