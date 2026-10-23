"""Local-only reservation-list amounts, currencies, privacy and query budget."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from apps.payments.models import OfflinePaymentReceipt, PaymentAttempt
from apps.reservations.models import BookingIntent, RefundObligation, Reservation
from apps.reservations.services.booking_list_summary import attach_booking_list_summaries
from apps.reservations.services.hostaway_booking import prepare_local_reservation
from tests.test_booking_models_services import make_property
from tests.test_booking_modifications_phase6 import create_extension
from tests.test_hostaway_booking_phase5 import make_intent

pytestmark = pytest.mark.django_db


@pytest.fixture
def booking_factory():
    property_obj = make_property()

    def create(**changes):
        reservation = prepare_local_reservation(make_intent(property_obj=property_obj))
        for key, value in changes.items():
            setattr(reservation, key, value)
        reservation.check_out = reservation.check_in + timedelta(days=reservation.nights)
        reservation.save()
        return reservation

    return create


def payment(booking, **changes):
    values = {
        "booking_intent": booking.booking_intent,
        "provider": "hyperpay",
        "amount": Decimal("700"),
        "currency": "SAR",
        "status": PaymentAttempt.Status.SUCCEEDED,
        "provider_payment_id": f"synthetic-summary-{uuid4()}",
        "verified_at": timezone.now(),
        "idempotency_key": str(uuid4()),
    }
    values.update(changes)
    return PaymentAttempt.objects.create(**values)


def money(totals):
    return {item.currency: item.amount for item in totals}


def receipt(booking, **changes):
    values = {
        "reservation": booking,
        "amount": Decimal("700"),
        "currency": "SAR",
        "method": "bank_transfer",
        "reference": str(uuid4()),
        "received_at": timezone.now(),
        "recorded_by": get_user_model().objects.create_user(username=str(uuid4())),
        "snapshot_digest": "0" * 64,
    }
    values.update(changes)
    return OfflinePaymentReceipt.objects.create(**values)


def refund(booking, **changes):
    values = {
        "reservation": booking,
        "reason": RefundObligation.Reason.MODIFICATION_DECREASE,
        "amount": Decimal("100"),
        "currency": "SAR",
        "status": RefundObligation.Status.TRANSFERRED,
        "transferred_at": timezone.now(),
    }
    values.update(changes)
    return RefundObligation.objects.create(**values)


def test_documented_collections_include_equal_installments_and_modification_surcharge(
    booking_factory,
):
    booking = booking_factory(total_price=Decimal("1500"), nights=3)
    payment(booking)
    payment(booking)
    booking.normalized_status = "confirmed"
    booking.hostaway_reservation_id = 99001
    booking.save()
    modification = create_extension(booking).request
    assert modification is not None
    payment(booking, amount=Decimal("100"), modification_request=modification)

    attach_booking_list_summaries([booking])

    assert money(booking.admin_paid_totals) == {"SAR": Decimal("1500")}
    assert booking.admin_average_nightly_price == Decimal("500")


@pytest.mark.parametrize(
    "changes",
    [
        {"status": status}
        for status in ("created", "pending", "failed", "cancelled", "expired", "review", "unknown")
    ]
    + [
        {"verified_at": None},
        {"verified_at": timezone.now() + timedelta(days=2)},
        {"provider_payment_id": None},
        {"provider_payment_id": ""},
        {"provider": "sandbox"},
        {"provider_result_code": "000.100.112"},
    ],
)
def test_unconfirmed_or_test_attempts_are_not_receipts(booking_factory, changes):
    booking = booking_factory(payment_status="paid")
    payment(booking, **changes)

    attach_booking_list_summaries([booking])

    assert booking.admin_paid_totals == ()
    assert booking.admin_payment_note == "لا يوجد مبلغ موثق محليًا"


@pytest.mark.parametrize("status", ["succeeded", "partially_refunded", "refunded"])
def test_collections_are_gross_and_only_completed_refunds_are_shown(booking_factory, status):
    booking = booking_factory()
    payment(booking, status=status)
    refund(booking)
    refund(booking, status="due", transferred_at=None, amount=Decimal("300"))
    refund(booking, status="processing", transferred_at=None, amount=Decimal("200"))
    refund(booking, status="cancelled", transferred_at=None, amount=Decimal("50"))
    refund(booking, transferred_at=timezone.now() + timedelta(days=2))

    attach_booking_list_summaries([booking])

    assert money(booking.admin_paid_totals) == {"SAR": Decimal("700")}
    assert money(booking.admin_refunded_totals) == {"SAR": Decimal("100")}


def test_linked_manual_receipt_is_counted_once(booking_factory):
    booking = booking_factory()
    attempt = payment(booking, provider="manual")
    receipt(booking, payment_attempt=attempt)

    attach_booking_list_summaries([booking])

    assert money(booking.admin_paid_totals) == {"SAR": Decimal("700")}


def test_unlinked_manual_receipt_is_counted(booking_factory):
    booking = booking_factory()
    receipt(booking)

    attach_booking_list_summaries([booking])

    assert money(booking.admin_paid_totals) == {"SAR": Decimal("700")}


def test_currencies_are_never_added_together_or_converted(booking_factory):
    booking = booking_factory(currency="MAD", total_price=Decimal("3001"), nights=3)
    payment(booking, amount=Decimal("500"))
    payment(booking, provider="manual", amount=Decimal("1000"), currency="MAD")
    refund(booking, amount=Decimal("20"), currency="MAD")

    attach_booking_list_summaries([booking])

    assert money(booking.admin_paid_totals) == {"SAR": Decimal("500"), "MAD": Decimal("1000")}
    assert money(booking.admin_refunded_totals) == {"MAD": Decimal("20")}
    assert booking.admin_average_nightly_price == Decimal("3001") / 3


def test_missing_receipts_points_and_test_bookings_are_not_fake_cash_totals(booking_factory):
    legacy = booking_factory(payment_status="paid")
    points = booking_factory(payment_status="paid_with_points")
    trial = booking_factory(is_test=True)
    payment(trial)

    attach_booking_list_summaries([legacy, points, trial])

    assert legacy.admin_paid_totals == ()
    assert legacy.admin_payment_note == "لا يوجد مبلغ موثق محليًا"
    assert points.admin_payment_note == "سداد بالنقاط"
    assert trial.admin_payment_note == "حجز تجريبي"
    assert trial.admin_paid_totals == ()


def test_zero_nights_is_defensive_and_zero_total_is_not_missing(booking_factory):
    booking = booking_factory(total_price=Decimal("0"))
    attach_booking_list_summaries([booking])
    assert booking.admin_average_nightly_price == Decimal("0")
    booking.nights = 0  # Corrupt legacy in-memory value; do not persist it.
    attach_booking_list_summaries([booking])
    assert booking.admin_average_nightly_price is None


def test_query_count_is_constant_and_summary_never_writes(booking_factory):
    bookings = [booking_factory() for _ in range(25)]
    payment(bookings[0])
    with CaptureQueriesContext(connection) as small:
        attach_booking_list_summaries(bookings[:1])
    with CaptureQueriesContext(connection) as large:
        attach_booking_list_summaries(bookings)
    assert len(small) == len(large) == 3
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in large)
    with CaptureQueriesContext(connection) as empty:
        attach_booking_list_summaries([])
    assert len(empty) == 0


@override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=False)
def test_owner_page_contains_localized_summary_in_table_and_mobile_cards(booking_factory):
    booking = booking_factory(
        total_price=Decimal("1500"),
        nights=3,
        normalized_status=Reservation.Status.CONFIRMED,
        hostaway_reservation_id=99003,
    )
    booking.property.name_ar = "شقة المعاينة الواسعة بحي الملقا"
    booking.property.save(update_fields=["name_ar"])
    payment(booking, amount=Decimal("1500"))
    client = Client()
    client.force_login(get_user_model().objects.create_superuser(username="summary-owner"))

    with translation.override("en"):
        page = client.get(reverse("notifications:booking_list"))

    assert page.status_code == 200
    content = page.content.decode()
    assert content.count("إجمالي المدفوع") == 2
    assert content.count("متوسط سعر الليلة") == 2
    assert content.count("شقة المعاينة الواسعة بحي الملقا") == 2
    assert "admin-booking-list.css" in content
    assert 'class="lsa-booking-card__summary"' in content
    assert 'class="money__amount">١٬٥٠٠٫٠٠</span>' in content
    assert 'class="money__amount">٥٠٠٫٠٠</span>' in content
    assert "money--en" not in content
    assert 'dir="ltr">Oct.' not in content


def test_booking_list_remains_private():
    response = Client().get(reverse("notifications:booking_list"))
    assert response.status_code == 302


@override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=False)
@pytest.mark.parametrize("query", ["", "Expired Sample", "+12025550100", "expired-list-proof"])
def test_expired_unpaid_local_request_never_appears_as_a_confirmed_stay(booking_factory, query):
    booking = booking_factory(public_reference="expired-list-proof")
    intent = booking.booking_intent
    intent.guest_first_name = "Expired Sample"
    intent.guest_phone = "+12025550100"
    intent.status = BookingIntent.Status.EXPIRED
    intent.expires_at = timezone.now() - timedelta(days=7)
    intent.save()
    payment(booking, status=PaymentAttempt.Status.PENDING, verified_at=None)
    client = Client()
    client.force_login(get_user_model().objects.create_superuser(username="expired-list-owner"))
    before = (
        Reservation.objects.count(),
        BookingIntent.objects.count(),
        PaymentAttempt.objects.count(),
    )
    page = client.get(reverse("notifications:booking_list"), {"q": query})
    assert page.status_code == 200 and page.context["bookings"] == []
    assert reverse("notifications:booking_detail", args=[booking.pk]) not in page.content.decode()
    booking.refresh_from_db()
    intent.refresh_from_db()
    assert booking.normalized_status == Reservation.Status.AWAITING_PAYMENT
    assert intent.status == BookingIntent.Status.EXPIRED
    assert before == (
        Reservation.objects.count(),
        BookingIntent.objects.count(),
        PaymentAttempt.objects.count(),
    )
    assert client.get(reverse("notifications:booking_detail", args=[booking.pk])).status_code == 200


@override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=False)
@pytest.mark.parametrize(
    "status", sorted(set(Reservation.Status.values) - Reservation.ACTIVE_STATUSES)
)
def test_hostaway_identifier_alone_never_makes_an_unconfirmed_record_a_real_stay(
    booking_factory, status
):
    booking = booking_factory(normalized_status=status, hostaway_reservation_id=99004)
    payment(booking)  # A receipt cannot substitute for Hostaway confirmation.
    client = Client()
    client.force_login(get_user_model().objects.create_superuser(username="unconfirmed-list-owner"))
    assert client.get(reverse("notifications:booking_list")).context["bookings"] == []


@override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=False)
@pytest.mark.parametrize("status", sorted(Reservation.ACTIVE_STATUSES))
@pytest.mark.parametrize("paid", [False, True])
def test_hostaway_confirmed_paid_and_unpaid_stays_remain_visible(booking_factory, status, paid):
    booking = booking_factory(normalized_status=status, hostaway_reservation_id=99005)
    # A real Hostaway booking remains valid after its original checkout form expires.
    intent = booking.booking_intent
    intent.status = BookingIntent.Status.EXPIRED
    intent.expires_at = timezone.now() - timedelta(days=7)
    intent.save()
    if paid:
        payment(booking)
    client = Client()
    client.force_login(get_user_model().objects.create_superuser(username="confirmed-list-owner"))
    page = client.get(reverse("notifications:booking_list"))
    assert [row.pk for row in page.context["bookings"]] == [booking.pk]
    assert page.context["bookings"][0].admin_status_code == (
        "completed" if paid else "awaiting_payment"
    )


@override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=False)
@pytest.mark.parametrize(
    "changes",
    [
        {"normalized_status": "confirmed", "hostaway_reservation_id": 99006, "is_test": True},
        {"normalized_status": "modified", "hostaway_reservation_id": None},
        {"normalized_status": "modified", "hostaway_reservation_id": 0},
    ],
)
def test_test_records_and_missing_hostaway_references_are_hidden(booking_factory, changes):
    booking_factory(**changes)
    client = Client()
    client.force_login(get_user_model().objects.create_superuser(username="proof-list-owner"))
    assert client.get(reverse("notifications:booking_list")).context["bookings"] == []


@override_settings(HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=False)
def test_hostaway_confirmed_points_payment_is_not_labelled_awaiting_payment(booking_factory):
    booking = booking_factory(
        normalized_status="confirmed",
        hostaway_reservation_id=99007,
        payment_status="paid_with_points",
    )
    client = Client()
    client.force_login(get_user_model().objects.create_superuser(username="points-list-owner"))
    page = client.get(reverse("notifications:booking_list"))
    assert [row.pk for row in page.context["bookings"]] == [booking.pk]
    assert page.context["bookings"][0].admin_status_code == "completed"
