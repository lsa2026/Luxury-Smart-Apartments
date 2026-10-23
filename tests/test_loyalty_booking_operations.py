"""Synthetic stays and receipts only; never contact a payment or inventory provider."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.loyalty.models import Ledger
from apps.loyalty.services import balance, reconcile
from apps.loyalty.stay import departure_plan
from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingModificationRequest, RefundObligation, Reservation
from apps.reservations.phone_search import bookings_for_phone
from tests.test_loyalty_stage8 import booking, join, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def programme(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "synthetic-stay-v1"
    settings.LOYALTY_TERMS_REFERENCE = "/synthetic-terms/"
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = False
    settings.HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED = False
    settings.BOOKING_CANCELLATION_REQUEST_ENABLED = True
    settings.BOOKING_AUTOMATIC_CANCELLATION_ENABLED = True
    clock = [datetime(2026, 10, 10, 13, tzinfo=UTC)]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    return clock


@pytest.fixture
def owner_client():
    owner = get_user_model().objects.create_superuser(username="synthetic-stay-owner")
    c = Client()
    c.force_login(owner)
    return c


def endpoint(r):
    return reverse("loyalty:booking_stay", args=[r.pk])


def stay(*, arrived=True, paid=True):
    guest = user("stay-guest")
    member = join(guest)
    r = booking(guest, arrived=arrived, paid=paid)
    quote = r.booking_intent.quote
    quote.night_prices = {
        "currency": "SAR",
        "reference": "synthetic-booking-offer",
        "nights": [
            {"date": r.check_in.isoformat(), "value": "400.00"},
            {"date": (r.check_in + timedelta(days=1)).isoformat(), "value": "350.00"},
        ],
    }
    quote.save(update_fields=["night_prices"])
    r.booking_intent.guest_phone = "+966501234567"
    r.booking_intent.save(update_fields=["guest_phone"])
    if arrived:
        reconcile(r.pk)
        r.refresh_from_db()
    return r, member


@pytest.mark.parametrize(
    "phone", ["0501234567", "٠٥٠١٢٣٤٥٦٧", "+966 50 123 4567", "00966501234567"]
)
def test_phone_lookup_normalizes_saudi_forms_without_network(owner_client, phone):
    r, _ = stay()
    r.is_test = False
    r.hostaway_reservation_id = 99008
    r.save(update_fields=["is_test", "hostaway_reservation_id"])
    with patch("httpx.Client.send", side_effect=AssertionError("No network on lookup")):
        response = owner_client.get(reverse("loyalty:admin"), {"phone": phone})
        assert [b.pk for b in response.context["booking_matches"]] == [r.pk]
        response = owner_client.get(reverse("notifications:booking_list"), {"q": phone})
        assert [b.pk for b in response.context["bookings"]] == [r.pk]
    assert b"booking_stay" not in response.content  # Actions belong to the selected booking.


@pytest.mark.parametrize(
    "stored,query",
    [
        ("+16462817246", "0016462817246"),
        ("+1 (646) 281-7246", "+16462817246"),
    ],
)
def test_international_phone_lookup(stored, query):
    r, _ = stay()
    r.booking_intent.guest_phone = stored
    r.booking_intent.save(update_fields=["guest_phone"])
    assert list(bookings_for_phone(Reservation.objects.all(), query)) == [r]
    assert not bookings_for_phone(Reservation.objects.all(), "+966501234568").exists()


def test_simple_admin_removes_advanced_forms_and_booking_has_bound_controls(owner_client):
    r, _ = stay(arrived=False)
    response = owner_client.get(reverse("loyalty:admin"))
    html = response.content.decode()
    assert "ابحث عن الضيف برقم الجوال" in html
    for name in ("arrival-booking", "settlement-booking", "nights-detail", "recover-booking"):
        assert name not in html
    response = owner_client.get(reverse("notifications:booking_detail", args=[r.pk]))
    assert "تأكيد وصول الضيف الآن" in response.content.decode()
    assert endpoint(r) in response.content.decode()
    assert not r.loyalty_state.get("arrival")


def test_arrival_is_one_click_and_retry_does_not_duplicate_points(owner_client):
    r, member = stay(arrived=False)
    assert owner_client.get(endpoint(r)).status_code == 405
    with patch("httpx.Client.send", side_effect=AssertionError("No provider on arrival")):
        assert owner_client.post(endpoint(r), {"action": "arrival"}).status_code == 302
        assert owner_client.post(endpoint(r), {"action": "arrival"}).status_code == 302
    r.refresh_from_db()
    assert r.loyalty_state["arrival"]["at"] == timezone.now().isoformat()
    assert balance(member) == Decimal("22.50")
    assert Ledger.objects.filter(member=member, reservation=r).count() == 1


def test_unpaid_arrival_never_creates_points(owner_client):
    r, member = stay(arrived=False, paid=False)
    owner_client.post(endpoint(r), {"action": "arrival"})
    r.refresh_from_db()
    assert r.loyalty_state.get("arrival")
    assert balance(member) == 0


@pytest.mark.parametrize("hour,total", [(19, "750.00"), (20, "350.00")])
def test_end_preview_is_read_only_and_uses_saved_nightly_prices(
    owner_client, programme, hour, total
):
    r, member = stay()
    programme[0] = programme[0].replace(hour=hour - 3)
    original = r.loyalty_state.copy()
    before = (Ledger.objects.count(), PaymentAttempt.objects.count(), balance(member))
    with patch("httpx.Client.send", side_effect=AssertionError("No provider on preview")):
        response = owner_client.post(endpoint(r), {"action": "preview"})
    assert response.status_code == 200
    assert response.context["plan"]["refundable_total"] == total
    assert response.context["confirmation"]
    r.refresh_from_db()
    assert r.loyalty_state == original
    assert (Ledger.objects.count(), PaymentAttempt.objects.count(), balance(member)) == before
    assert not BookingModificationRequest.objects.exists() and not RefundObligation.objects.exists()


def test_early_end_requires_confirmation_and_enters_existing_cancellation_once(
    owner_client, programme
):
    r, member = stay()
    programme[0] = programme[0].replace(hour=17)  # Eight in the property's timezone.
    token = owner_client.post(endpoint(r), {"action": "preview"}).context["confirmation"]
    owner_client.post(endpoint(r), {"action": "end", "confirmation": token})
    r.refresh_from_db()
    assert not r.loyalty_state.get("departure_settlement")
    with patch("httpx.Client.send", side_effect=AssertionError("No provider before refund review")):
        result = owner_client.post(
            endpoint(r),
            {
                "action": "end",
                "confirmation": token,
                "confirm_vacated": "on",
            },
        )
        owner_client.post(
            endpoint(r),
            {
                "action": "end",
                "confirmation": token,
                "confirm_vacated": "on",
            },
        )
    cancellation = BookingModificationRequest.objects.get()
    assert result.url == reverse("notifications:cancellation_detail", args=[cancellation.pk])
    r.refresh_from_db()
    assert r.loyalty_state["departure_settlement"]["cash_refund"] == "350.00"
    assert r.normalized_status == Reservation.Status.CONFIRMED
    assert balance(member) == Decimal(
        "22.50"
    )  # No correction until external cancellation confirms.
    assert not RefundObligation.objects.exists()


def test_normal_end_needs_no_nightly_input_and_does_not_cancel_or_refund(owner_client, programme):
    r, member = stay()
    r.booking_intent.quote.night_prices = {}
    r.booking_intent.quote.save(update_fields=["night_prices"])
    programme[0] += timedelta(days=3)
    preview = owner_client.post(endpoint(r), {"action": "preview"})
    assert preview.context["plan"]["normal"] is True
    assert Decimal(preview.context["plan"]["cash_refund"]) == 0
    owner_client.post(
        endpoint(r),
        {
            "action": "end",
            "confirmation": preview.context["confirmation"],
            "confirm_vacated": "on",
        },
    )
    r.refresh_from_db()
    assert r.loyalty_state["departure_settlement"]["normal"] is True
    assert balance(member) == Decimal("22.50")
    assert not BookingModificationRequest.objects.exists() and not RefundObligation.objects.exists()


def test_changed_payment_rejects_stale_confirmation(owner_client):
    r, _ = stay()
    token = owner_client.post(endpoint(r), {"action": "preview"}).context["confirmation"]
    PaymentAttempt.objects.filter(booking_intent=r.booking_intent).update(status="pending")
    owner_client.post(
        endpoint(r), {"action": "end", "confirmation": token, "confirm_vacated": "on"}
    )
    r.refresh_from_db()
    assert not r.loyalty_state.get("departure_settlement")
    assert not BookingModificationRequest.objects.exists()


def test_missing_historical_nights_block_guessing_and_wrong_actor_cannot_confirm(owner_client):
    r, _ = stay()
    q = r.booking_intent.quote
    q.night_prices = {}
    q.save(update_fields=["night_prices"])
    with pytest.raises(ValidationError):
        departure_plan(r, vacated_at=timezone.now())
    assert owner_client.post(endpoint(r), {"action": "preview"}).status_code == 302
    other = Client()
    other.force_login(user("unprivileged"))
    assert other.post(endpoint(r), {"action": "arrival"}).status_code == 302


def test_bad_signature_private_scope_and_disabled_programme_remain_closed(owner_client, settings):
    r, _ = stay()
    assert (
        owner_client.post(
            endpoint(r),
            {
                "action": "end",
                "confirmation": "tampered",
                "confirm_vacated": "on",
            },
        ).status_code
        == 302
    )
    settings.LOYALTY_PILOT_ONLY = True
    settings.LOYALTY_PILOT_EMAILS = ["other@example.invalid"]
    assert owner_client.post(endpoint(r), {"action": "arrival"}).status_code == 403
    settings.LOYALTY_ENABLED = False
    assert owner_client.post(endpoint(r), {"action": "arrival"}).status_code == 404


def test_csrf_guard_is_not_bypassed(owner_client):
    r, _ = stay()
    client = Client(enforce_csrf_checks=True)
    client.force_login(get_user_model().objects.get(username="synthetic-stay-owner"))
    assert client.post(endpoint(r), {"action": "arrival"}).status_code == 403


def test_early_end_correction_runs_after_existing_refund_workflow(owner_client, programme):
    from types import SimpleNamespace

    from django.test import override_settings

    from apps.payments.hyperpay.refunds import HyperPayRefundService
    from apps.reservations.services.automatic_modifications import execute_automatic_modification
    from tests.test_hyperpay_refunds_phase94 import REFUND_SETTINGS, RefundStub

    r, member = stay()
    programme[0] = programme[0].replace(hour=17)
    preview = owner_client.post(endpoint(r), {"action": "preview"})
    owner_client.post(
        endpoint(r),
        {
            "action": "end",
            "confirmation": preview.context["confirmation"],
            "confirm_vacated": "on",
        },
    )
    cancellation = BookingModificationRequest.objects.get()
    calls = []

    def confirmed_cancellation(request):
        calls.append("cancel")
        request.status = request.Status.COMPLETED
        request.save()
        request.reservation.normalized_status = Reservation.Status.CANCELLED
        request.reservation.save()
        return SimpleNamespace(code="completed", request=request)

    stub = RefundStub({"id": "SYNTHETIC-REFUND", "result": {"code": "000.100.110"}})
    with override_settings(
        **{
            **REFUND_SETTINGS,
            "BOOKING_AUTOMATIC_REFUND_ENABLED": True,
            "BOOKING_AUTOMATIC_CANCELLATION_ENABLED": True,
        }
    ):
        outcome = execute_automatic_modification(
            cancellation,
            service=SimpleNamespace(execute=confirmed_cancellation),
            refund_service=HyperPayRefundService(client=stub),
        )
    assert outcome.code == "completed" and calls == ["cancel"]
    assert len(stub.calls) == 1
    assert r.refund_obligations.get().amount == Decimal("350.00")
    reconcile(r.pk)
    assert balance(member) == Decimal("12.00")
    reconcile(r.pk)
    assert balance(member) == Decimal("12.00")
