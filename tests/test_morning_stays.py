"""Morning stays: disposable DB and fake providers; never a real booking or charge."""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import pytest
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.core.guest_documents import documents_digest
from apps.core.payment_log_filter import PaymentCapabilityFilter
from apps.payments.hyperpay.exceptions import HyperPayCheckoutError, HyperPayConnectionError
from apps.payments.models import MorningPaymentAttempt, PaymentAttempt
from apps.payments.morning_payments import create_checkout, public_url, verify_payment
from apps.payments.views import _hyperpay_return_token
from apps.properties.models import Property
from apps.reservations.models import BookingIntent, MorningStay, Reservation
from apps.reservations.morning_stays import (
    ZONE,
    MorningStayError,
    create_stay,
    overnight_conflicts,
    validate_window,
    verify_window,
)
from apps.reservations.morning_views import MorningStayForm
from tests.test_availability_service import make_day, make_property
from tests.test_hyperpay import HYPERPAY_SETTINGS, HyperPayStub
from tests.test_manual_booking_operations_phase61 import owner
from tests.test_reservation_payment_links import billing, form_data

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated(settings):
    for key, value in HYPERPAY_SETTINGS.items():
        setattr(settings, key, value)
    settings.SITE_BASE_URL = "https://testserver"
    cache.clear()
    with patch("httpx.Client.request", side_effect=AssertionError("unexpected live request")):
        yield


def window():
    day = timezone.now().astimezone(ZONE).date() + timedelta(days=2)
    return datetime.combine(day, time(1), ZONE), datetime.combine(day, time(11), ZONE)


def property_and_remote():
    p = make_property()
    p.hostaway_listing_map_id = 97001
    p.save()
    start, _ = window()
    day = start.date()
    remote = Mock()
    remote.get_listing_calendar.return_value = SimpleNamespace(
        days=tuple(make_day(day + timedelta(days=i)) for i in (-1, 0, 1))
    )
    remote.guest_stay_page.return_value = []
    return p, remote


def create(p=None, remote=None, actor=None, request_id=None):
    if p is None:
        p, remote = property_and_remote()
    start, end = window()
    return create_stay(
        actor=actor or owner(),
        request_id=request_id or uuid.uuid4(),
        property_obj=p,
        starts_at=start,
        ends_at=end,
        guest_name="Synthetic Guest",
        guest_phone="+16502530000",
        guest_email="morning@example.test",
        guests=2,
        amount_sar=Decimal("500"),
        client=remote,
    )


def consent():
    return {
        "documents_digest": documents_digest(),
        "consents": {"terms": True, "house_rules": True},
    }


def start_payment(stay, remote, gateway):
    return create_checkout(
        stay.pk, billing_data=billing(), acceptance=consent(), client=gateway, hostaway=remote
    )


def approved(attempt, **changes):
    return {
        "id": "morning-financial-123456789",
        "result": {"code": "000.000.000"},
        "amount": str(attempt.amount),
        "currency": "SAR",
        "paymentType": "DB",
        "merchantTransactionId": attempt.merchant_transaction_id,
        "paymentBrand": "VISA",
        **changes,
    }


@pytest.mark.parametrize("start_hour,end_hour", [(0, 11), (1, 12), (11, 10), (13, 14)])
def test_invalid_or_noon_windows_are_rejected(start_hour, end_hour):
    start, end = window()
    with pytest.raises(MorningStayError):
        validate_window(start.replace(hour=start_hour), end.replace(hour=end_hour))


def test_admin_form_normalizes_phone_and_requires_full_identity():
    p, _ = property_and_remote()
    start, end = window()
    data = dict(
        request_id=uuid.uuid4(),
        property=p.pk,
        date=start.date(),
        start_time="01:00",
        end_time="11:00",
        guest_name="Synthetic Guest",
        guest_phone="+1 650 253 0000",
        guest_email="morning@example.test",
        guests=2,
        amount_sar="500.00",
    )
    form = MorningStayForm(data)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_phone"] == "+16502530000"
    assert form.cleaned_data["starts_at"] == start
    assert not MorningStayForm(data | {"guest_name": "OnlyName"}).is_valid()
    assert not MorningStayForm(data | {"guest_email": "bad"}).is_valid()


def test_create_is_idempotent_blocks_parallel_stays_and_never_creates_hostaway_booking():
    p, remote = property_and_remote()
    actor = owner()
    key = uuid.uuid4()
    stay = create(p, remote, actor, key)
    assert create(p, remote, actor, key).pk == stay.pk
    with pytest.raises(MorningStayError, match="آخر"):
        create(p, remote, actor)
    remote.create_reservation_with_price_details.assert_not_called()
    assert not Reservation.objects.exists()
    assert not BookingIntent.objects.exists()
    assert not PaymentAttempt.objects.exists()


@pytest.mark.parametrize("kind", ["departing_today", "extended", "early_arrival", "owner_stay"])
def test_every_overlapping_stay_blocks_even_without_a_noon_departure(kind):
    p, remote = property_and_remote()
    start, end = window()
    day = start.date()
    row = {
        "id": 1,
        "listingMapId": p.hostaway_listing_map_id,
        "status": "new",
        "arrivalDate": (day - timedelta(days=2)).isoformat(),
        "departureDate": day.isoformat(),
        "checkInTime": 15,
        "checkOutTime": 12,
    }
    if kind == "extended":
        row["departureDate"] = (day + timedelta(days=3)).isoformat()
    elif kind == "early_arrival":
        row.update(
            arrivalDate=day.isoformat(),
            departureDate=(day + timedelta(days=1)).isoformat(),
            checkInTime=8,
        )
    elif kind == "owner_stay":
        row["status"] = "ownerStay"
    remote.guest_stay_page.return_value = [row]
    with pytest.raises(MorningStayError, match="إشغال"):
        verify_window(p, start, end, client=remote)


def test_new_afternoon_booking_is_not_a_conflict_but_maintenance_is():
    p, remote = property_and_remote()
    start, end = window()
    day = start.date()
    remote.guest_stay_page.return_value = [
        {
            "id": 1,
            "listingMapId": p.hostaway_listing_map_id,
            "status": "new",
            "arrivalDate": day.isoformat(),
            "departureDate": (day + timedelta(days=1)).isoformat(),
            "checkInTime": 15,
            "checkOutTime": 12,
        }
    ]
    rows = remote.get_listing_calendar.return_value.days
    remote.get_listing_calendar.return_value.days = (
        rows[0],
        replace(rows[1], is_available=False),
        rows[2],
    )
    assert verify_window(p, start, end, client=remote)["source"] == "hostaway_read_only"
    remote.get_listing_calendar.return_value.days = (
        rows[0],
        replace(rows[1], status="blocked"),
        rows[2],
    )
    with pytest.raises(MorningStayError, match="حجب"):
        verify_window(p, start, end, client=remote)


def test_incomplete_or_multiunit_calendar_cannot_authorize_collection():
    p, remote = property_and_remote()
    start, end = window()
    rows = remote.get_listing_calendar.return_value.days
    remote.get_listing_calendar.return_value.days = rows[1:]
    with pytest.raises(MorningStayError):
        verify_window(p, start, end, client=remote)
    remote.get_listing_calendar.return_value.days = (
        replace(rows[0], count_available_units=3),
        *rows[1:],
    )
    with pytest.raises(MorningStayError, match="وحدات"):
        verify_window(p, start, end, client=remote)


def test_normal_nightly_stays_respect_the_internal_morning_allocation():
    stay = create()
    day = stay.starts_at.date()
    assert overnight_conflicts(stay.property, day - timedelta(days=1), day)
    assert not overnight_conflicts(stay.property, day, day + timedelta(days=1))


def test_guest_consent_revalidation_and_one_gateway_claim():
    p, remote = property_and_remote()
    stay = create(p, remote)
    gateway = HyperPayStub()
    with pytest.raises(HyperPayCheckoutError, match="acceptance"):
        create_checkout(
            stay.pk, billing_data=billing(), acceptance={}, client=gateway, hostaway=remote
        )
    attempt = start_payment(stay, remote, gateway)
    assert start_payment(stay, remote, gateway).pk == attempt.pk
    assert gateway.checkout_calls == 1
    assert gateway.payload["amount"] == "500.00"
    assert gateway.payload["customer.givenName"] == "Synthetic"
    stay.refresh_from_db()
    assert stay.legal_acceptance["consents"]["house_rules"] is True
    assert stay.status == "processing"
    assert not Reservation.objects.exists()


def test_timeout_keeps_allocation_and_blocks_replacement_even_after_lease_expiry():
    p, remote = property_and_remote()
    actor = owner()
    stay = create(p, remote, actor)
    gateway = HyperPayStub()
    gateway.create_checkout = Mock(side_effect=HyperPayConnectionError())
    with pytest.raises(HyperPayCheckoutError, match="uncertain"):
        start_payment(stay, remote, gateway)
    MorningStay.objects.filter(pk=stay.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    with pytest.raises(MorningStayError):
        create(p, remote, actor)
    assert MorningPaymentAttempt.objects.count() == 1


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "succeeded"),
        ({"amount": "1"}, "review"),
        ({"currency": "USD"}, "review"),
        ({"id": ""}, "review"),
        ({"merchantTransactionId": "other"}, "review"),
    ],
)
def test_server_verification_binds_amount_currency_reference_and_financial_id(changes, expected):
    p, remote = property_and_remote()
    stay = create(p, remote)
    gateway = HyperPayStub()
    attempt = start_payment(stay, remote, gateway)
    gateway.payment_document = approved(attempt, **changes)
    assert verify_payment(attempt.pk, client=gateway).status == expected
    stay.refresh_from_db()
    assert stay.status == ("paid" if expected == "succeeded" else "review")
    if expected == "succeeded":
        verify_payment(attempt.pk, client=gateway)
        assert gateway.payment_calls == 1
    remote.create_reservation_with_price_details.assert_not_called()
    assert not Reservation.objects.exists()


def test_late_verified_payment_is_retained_for_admin_review():
    p, remote = property_and_remote()
    stay = create(p, remote)
    gateway = HyperPayStub()
    attempt = start_payment(stay, remote, gateway)
    gateway.payment_document = approved(attempt)
    with patch(
        "apps.payments.morning_payments.timezone.now",
        return_value=stay.ends_at + timedelta(minutes=1),
    ):
        assert verify_payment(attempt.pk, client=gateway).status == "succeeded"
    stay.refresh_from_db()
    assert stay.paid_at and stay.status == "review"


def test_payment_page_does_not_charge_on_get_and_requires_csrf_and_acceptance():
    stay = create()
    url = urlsplit(public_url(stay)).path
    client = Client(enforce_csrf_checks=True)
    page = client.get(url)
    assert page.status_code == 200
    assert "no-store" in page["Cache-Control"]
    assert "التدخين" in page.content.decode()
    assert client.post(url, form_data()).status_code == 403
    assert not MorningPaymentAttempt.objects.exists()
    assert Client().post(url, billing()).status_code == 200
    assert not MorningPaymentAttempt.objects.exists()


def test_guest_payment_uses_existing_wallet_ui_and_signed_result_without_booking_access():
    p, remote = property_and_remote()
    stay = create(p, remote)
    gateway = HyperPayStub()
    url = urlsplit(public_url(stay)).path
    with patch(
        "apps.payments.morning_views.create_checkout",
        side_effect=lambda *a, **k: create_checkout(*a, **k, client=gateway, hostaway=remote),
    ):
        page = Client().post(url, form_data(), secure=True)
    assert page.status_code == 200
    assert b'data-brands="MADA"' in page.content
    assert b'data-brands="VISA MASTER"' in page.content
    attempt = MorningPaymentAttempt.objects.get()
    result_url = reverse("payments:morning_result", args=[attempt.pk])
    assert Client().get(result_url).status_code == 404
    gateway.payment_document = approved(attempt)
    with patch(
        "apps.payments.morning_views.verify_payment",
        side_effect=lambda pk: verify_payment(pk, client=gateway),
    ):
        result = Client().get(result_url, {"return_token": _hyperpay_return_token(attempt)})
    assert result.status_code == 200
    assert "استلام دفعتك" in result.content.decode()
    assert not Reservation.objects.exists()


def test_admin_navigation_and_real_create_form_are_private():
    p, remote = property_and_remote()
    actor = owner()
    client = Client()
    url = reverse("notifications:morning_create")
    assert client.get(url).status_code == 302
    client.force_login(actor)
    hub = client.get(reverse("notifications:hub"))
    assert url.encode() not in hub.content
    bookings = client.get(reverse("notifications:booking_list"))
    assert url.encode() in bookings.content
    page = client.get(url)
    key = page.context["form"].initial["request_id"]
    start, _ = window()
    with patch("apps.reservations.morning_stays.HostawayClient") as factory:
        factory.return_value.__enter__.return_value = remote
        response = client.post(
            url,
            dict(
                request_id=key,
                property=p.pk,
                date=start.date(),
                start_time="01:00",
                end_time="11:00",
                guest_name="Synthetic Guest",
                guest_phone="+16502530000",
                guest_email="morning@example.test",
                guests=2,
                amount_sar="500",
            ),
        )
    assert response.status_code == 302
    assert client.get(response.url).status_code == 200
    assert not Reservation.objects.exists()


def test_morning_link_is_redacted_from_operational_logs():
    record = logging.LogRecord(
        "test", logging.INFO, "", 1, "GET /payments/morning-pay/secret-token/", (), None
    )
    PaymentCapabilityFilter().filter(record)
    assert "secret-token" not in record.getMessage()


def availability_query():
    start, _ = window()
    return dict(date=start.date().isoformat(), start_time="01:00", end_time="11:00", guests=2)


def test_morning_availability_is_private_read_only_and_reuses_live_pages():
    p, remote = property_and_remote()
    second = Property.objects.create(
        hostaway_listing_id=9002,
        hostaway_listing_map_id=97002,
        slug="second-morning-property",
        name_ar="وحدة ثانية",
        person_capacity=4,
        is_visible=True,
        hostaway_is_active=True,
    )
    client = Client()
    url = reverse("notifications:morning_available_properties")
    with patch("apps.reservations.morning_views.HostawayClient") as factory:
        factory.return_value.__enter__.return_value = remote
        assert client.get(url, availability_query()).status_code == 302
        client.force_login(owner())
        assert client.post(url, availability_query()).status_code == 405
        assert client.get(reverse("notifications:morning_create")).status_code == 200
        factory.assert_not_called()
        page = client.get(url, availability_query())
    assert page.status_code == 200
    result = page.json()
    assert {row["id"] for row in result["properties"]} == {str(p.pk), str(second.pk)}
    assert all(row["time_zone"] == "Asia/Riyadh" for row in result["properties"])
    assert result["unverified_count"] == 0
    assert "no-store" in page["Cache-Control"]
    assert remote.get_listing_calendar.call_count == 2
    assert remote.guest_stay_page.call_count == 1
    assert not MorningStay.objects.exists()
    assert not MorningPaymentAttempt.objects.exists()
    assert not PaymentAttempt.objects.exists()
    assert not Reservation.objects.exists()
    assert not BookingIntent.objects.exists()
    assert {call[0] for call in remote.method_calls} == {"get_listing_calendar", "guest_stay_page"}


def test_morning_availability_excludes_existing_internal_allocation():
    p, remote = property_and_remote()
    actor = owner()
    create(p, remote, actor=actor)
    remote.reset_mock()
    client = Client()
    client.force_login(actor)
    with patch("apps.reservations.morning_views.HostawayClient") as factory:
        factory.return_value.__enter__.return_value = remote
        page = client.get(
            reverse("notifications:morning_available_properties"), availability_query()
        )
    assert page.json()["properties"] == []
    remote.get_listing_calendar.assert_not_called()
    assert MorningStay.objects.count() == 1


@pytest.mark.parametrize("kind", ["overlap", "maintenance", "capacity", "afternoon"])
def test_morning_availability_uses_real_period_checks(kind):
    p, remote = property_and_remote()
    start, _ = window()
    if kind in {"overlap", "afternoon"}:
        remote.guest_stay_page.return_value = [
            {
                "id": 9,
                "listingMapId": p.hostaway_listing_map_id,
                "status": "new",
                "arrivalDate": (
                    start.date() if kind == "afternoon" else start.date() - timedelta(days=1)
                ).isoformat(),
                "departureDate": (start.date() + timedelta(days=1)).isoformat(),
                "checkInTime": 15,
                "checkOutTime": 12,
            }
        ]
    if kind == "maintenance":
        rows = remote.get_listing_calendar.return_value.days
        remote.get_listing_calendar.return_value.days = (
            replace(rows[0], status="maintenance"),
            *rows[1:],
        )
    if kind == "capacity":
        p.person_capacity = 1
        p.save()
    client = Client()
    client.force_login(owner())
    with patch("apps.reservations.morning_views.HostawayClient") as factory:
        factory.return_value.__enter__.return_value = remote
        result = client.get(
            reverse("notifications:morning_available_properties"), availability_query()
        ).json()
    assert bool(result["properties"]) == (kind == "afternoon")
    assert not MorningStay.objects.exists()


def test_morning_availability_does_not_claim_unverified_units_are_free():
    from apps.integrations.hostaway.exceptions import HostawayTimeoutError

    _, remote = property_and_remote()
    remote.get_listing_calendar.side_effect = HostawayTimeoutError("synthetic timeout")
    client = Client()
    client.force_login(owner())
    with patch("apps.reservations.morning_views.HostawayClient") as factory:
        factory.return_value.__enter__.return_value = remote
        result = client.get(
            reverse("notifications:morning_available_properties"), availability_query()
        ).json()
    assert result["properties"] == [] and result["unverified_count"] == 1
    assert "synthetic timeout" not in str(result)


def test_morning_availability_reports_property_local_time_and_rejects_past_search():
    from zoneinfo import ZoneInfo

    p, remote = property_and_remote()
    p.country_code = "MA"
    p.time_zone_name = "Africa/Casablanca"
    p.save()
    client = Client()
    client.force_login(owner())
    url = reverse("notifications:morning_available_properties")
    with patch("apps.reservations.morning_views.HostawayClient") as factory:
        factory.return_value.__enter__.return_value = remote
        result = client.get(url, availability_query()).json()
        remote.reset_mock()
        past = client.get(
            url,
            availability_query()
            | {"date": (timezone.now().date() - timedelta(days=1)).isoformat()},
        ).json()
    assert result["properties"][0]["time_zone"] == "Africa/Casablanca"
    assert datetime.fromisoformat(result["properties"][0]["starts_at"]) == datetime.combine(
        window()[0].date(), time(1), ZoneInfo("Africa/Casablanca")
    )
    assert past["properties"] == [] and past["detail"]
    remote.get_listing_calendar.assert_not_called()


@pytest.mark.parametrize("changes", [{"end_time": "12:00"}, {"date": "invalid"}, {"guests": 0}])
def test_invalid_morning_search_does_not_call_hostaway(changes):
    client = Client()
    client.force_login(owner())
    with patch("apps.reservations.morning_views.HostawayClient") as factory:
        page = client.get(
            reverse("notifications:morning_available_properties"), availability_query() | changes
        )
    assert page.status_code == 400
    factory.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_postgres_serializes_two_different_morning_requests():
    if connection.vendor != "postgresql":
        pytest.skip("Concurrent allocation requires PostgreSQL row locks")
    p, remote = property_and_remote()
    actor = owner()
    barrier = Barrier(2)
    remote.guest_stay_page.side_effect = lambda *a, **k: (barrier.wait(), [])[1]

    def submit():
        close_old_connections()
        try:
            create(p, remote, actor)
            return "created"
        except MorningStayError:
            return "blocked"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(outcomes) == ["blocked", "created"]
    assert MorningStay.objects.count() == 1
