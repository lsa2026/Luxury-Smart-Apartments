"""Fictional, offline reversals: signed accounting is not an overdraft facility."""

from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.loyalty.backup import LocalMirror, capture, validate
from apps.loyalty.models import Ledger
from apps.loyalty.services import (
    available,
    balance,
    held_points,
    hold,
    reconcile,
    redeem,
    transfer,
    withdraw,
)
from apps.loyalty.whatsapp_notices import movement_caption
from apps.reservations.models import Reservation
from tests.test_loyalty_stage8 import booking, completed_change, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def programme(settings, monkeypatch):
    settings.LOYALTY_ENABLED = True
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 10, 13, tzinfo=UTC))


def cancel(r):
    r.refresh_from_db()
    r.loyalty_state["departure_settlement"] = {
        "consumed_cash_lodging": "0.00",
        "reference": "TEST-FULL-CANCELLATION",
    }
    r.normalized_status = Reservation.Status.CANCELLED
    r.save()
    return reconcile(r.pk)


def example():
    a, b = user("signed-a"), user("signed-b")
    am, bm = join(a), join(b)
    r = booking(a, "957.00")
    reconcile(r.pk)
    transfer(a, "TEST-GIFT-THREE", bm.number, "3.00")
    return a, b, am, bm, r


def test_957_reward_gift_three_full_reversal_is_one_signed_correction():
    a, b, am, bm, r = example()
    before = list(Ledger.objects.values())
    assert balance(am) == Decimal("25.71")
    assert cancel(r)["movements"] == 1
    assert balance(am) == Decimal("-3.00")
    assert available(am) == 0 and held_points(am) == 0
    assert balance(bm) == Decimal("3.00")
    adjustment = Ledger.objects.latest("sequence")
    assert adjustment.debit == Decimal("28.71") and adjustment.original_id
    assert adjustment.member_balance_after == Decimal("-3.00")
    assert adjustment.program_balance_after == 0
    assert list(Ledger.objects.filter(pk__in=[v["id"] for v in before]).values()) == before
    assert reconcile(r.pk)["movements"] == 0
    with pytest.raises(ValidationError):
        transfer(a, "TEST-NO-OVERDRAFT", bm.number, "0.01")
    next_booking = booking(a, "100.00", paid=False, arrived=False)
    next_booking.normalized_status = Reservation.Status.AWAITING_PAYMENT
    next_booking.save()
    with pytest.raises(ValidationError):
        hold(a, "TEST-NO-NEW-HOLD", next_booking.pk, "0.01")
    reconcile(booking(a, "100.00").pk)
    assert balance(am) == 0
    reconcile(booking(a, "100.00").pk)
    assert balance(am) == available(am) == Decimal("3.00")


def test_new_incoming_gift_offsets_signed_balance_without_recipient_clawback():
    a, b, am, bm, r = example()
    cancel(r)
    transfer(b, "TEST-VOLUNTARY-GIFT-BACK", am.number, "2.00")
    assert balance(am) == Decimal("-1.00") and available(am) == 0
    assert balance(bm) == Decimal("1.00")


def test_existing_hold_is_honoured_after_reward_correction():
    a, b, am, bm, r = example()
    next_booking = booking(a, "100.00", paid=False, arrived=False)
    next_booking.normalized_status = Reservation.Status.AWAITING_PAYMENT
    next_booking.save()
    hold(a, "TEST-ALREADY-HELD", next_booking.pk, "20.00")
    cancel(r)
    assert balance(am) == Decimal("-3.00") and held_points(am) == Decimal("20.00")
    next_booking.refresh_from_db()
    next_booking.normalized_status = Reservation.Status.CONFIRMED
    next_booking.save()
    assert Decimal(redeem(a, "TEST-CONSUME-PRIOR-HOLD", next_booking.pk)["value"]) == 20
    assert balance(am) == Decimal("-23.00") and held_points(am) == 0
    assert available(am) == 0
    assert Decimal(redeem(a, "TEST-CONSUME-PRIOR-HOLD", next_booking.pk)["value"]) == 20
    assert balance(am) == Decimal("-23.00")


@pytest.mark.django_db(transaction=connection.vendor == "postgresql")
def test_signed_balances_backup_and_reopened_mirror(tmp_path):
    a, b, am, bm, r = example()
    # The recipient forfeits their own points; later reversal can make the
    # programme total negative too. Never fabricate a zero running balance.
    withdraw(b, "TEST-RECIPIENT-CLOSE")
    cancel(r)
    data = capture(source_id="test-signed-balance")
    result = validate(data)
    assert result["program_balance"] == "-3.00"
    assert result["member_balances"][str(am.pk)] == "-3.00"
    # LocalMirror deliberately refuses PostgreSQL sources; that engine proves
    # capture/validation here, while SQLite also rehearses the local mirror.
    if connection.vendor == "sqlite":
        path = tmp_path / "signed.sqlite3"
        LocalMirror(path).update(data)
        assert LocalMirror(path).read() == data
    assert withdraw(a, "TEST-NEGATIVE-CLOSE")["forfeited"] == "0.00"
    assert balance(am) == Decimal("-3.00")


@pytest.mark.django_db(transaction=connection.vendor == "postgresql")
def test_old_deferred_corrections_become_append_only_rows_once():
    a, b = user("legacy-a"), user("legacy-b")
    am, bm = join(a), join(b)
    r = booking(a)
    reconcile(r.pk)
    original = Ledger.objects.get(member=am, event_type="earn")
    transfer(a, "TEST-LEGACY-GIFT", bm.number, "22.50")
    am.deferred_corrections = [
        {
            "remaining": "4.50",
            "original": original.pk,
            "booking": str(r.pk),
            "component": original.component,
            "type": original.operation_type,
        }
    ]
    am.save(update_fields=["deferred_corrections"])
    migrate = import_module("apps.loyalty.migrations.0005_signed_correction_balances")
    editor = SimpleNamespace(
        connection=connection, execute=lambda sql: connection.cursor().execute(sql)
    )
    with transaction.atomic():
        migrate.materialize_deferred(apps, editor)
    assert balance(am) == Decimal("-4.50") and balance(bm) == Decimal("22.50")
    am.refresh_from_db()
    assert not am.deferred_corrections
    count = Ledger.objects.count()
    with transaction.atomic():
        migrate.materialize_deferred(apps, editor)
    assert Ledger.objects.count() == count
    completed_change(r, "600.00")
    assert reconcile(r.pk)["movements"] == 0
    assert balance(am) == Decimal("-4.50")
    validate(capture(source_id="legacy-signed"))


def test_negative_balance_page_explains_it_and_does_not_display_negative_holds(client, settings):
    a, b, am, bm, r = example()
    cancel(r)
    settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "test-signed"
    settings.LOYALTY_TERMS_REFERENCE = "/ar/legal/rewards/test-signed/"
    client.force_login(a)
    response = client.get(reverse("accounts:rewards"))
    assert response.status_code == 200
    assert response.context["loyalty_metrics"] == {
        "balance": Decimal("-3.00"),
        "available": Decimal(0),
        "held": Decimal(0),
    }
    assert "رصيدك سالب" in response.content.decode()
    with override_settings(SITE_BASE_URL="https://example.invalid"):
        caption = movement_caption(Ledger.objects.latest("sequence"), "ar")
        assert "-٣٫٠٠" in caption or "-٣.٠٠" in caption


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_signed_terms_are_new_version_and_join_records_it(client, settings, language):
    from apps.loyalty.accrual import (
        IDENTITY_TERMS_REFERENCE,
        IDENTITY_TERMS_VERSION,
        PROGRAM_TERMS_VERSION,
    )
    from apps.loyalty.models import Member
    from apps.loyalty.public_terms import document

    old = document(language, version=IDENTITY_TERMS_VERSION)
    settings.LOYALTY_UI_ENABLED = settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = IDENTITY_TERMS_VERSION
    settings.LOYALTY_TERMS_REFERENCE = IDENTITY_TERMS_REFERENCE
    new = document(language)
    assert new["version"] == PROGRAM_TERMS_VERSION and new["sha256"] != old["sha256"]
    old_response = client.get(f"/{language}/legal/rewards/2026-10-17-v3/")
    assert old_response["X-Document-SHA256"] == old["sha256"]
    new_response = client.get(f"/{language}/legal/rewards/2026-10-18-v5/")
    assert new_response["X-Document-SHA256"] == new["sha256"]
    a = user("new-consent-" + language)
    client.force_login(a)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.post(reverse("accounts:rewards"), {"action": "join", "consent": "on"})
    assert response.status_code == 302
    member = Member.objects.get(customer=a)
    assert member.terms_version == PROGRAM_TERMS_VERSION
    assert member.terms_reference == f"/{language}/legal/rewards/2026-10-18-v5/"
    assert member.consent_history[0]["terms_content_sha256"] == new["sha256"]
