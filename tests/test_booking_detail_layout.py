"""Approved presentation must retain real forms, permissions and financial guards."""

from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone, translation

from apps.notifications.models import GuestInvoiceDelivery
from apps.payments.models import OfflinePaymentReceipt, PaymentAttempt
from apps.payments.offline_receipts import receipt_snapshot
from apps.reservations.models import Reservation
from tests.test_booking_modifications_phase6 import confirmed_reservation, create_extension
from tests.test_operations_booking_summary import payment

pytestmark = pytest.mark.django_db


class PageElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


@pytest.fixture
def owner_client(client, settings):
    settings.HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED = False
    settings.RESERVATION_PAYMENT_LINKS_ENABLED = True
    settings.MANUAL_PAYMENT_RECEIPTS_ENABLED = True
    client.force_login(get_user_model().objects.create_superuser(username="detail-owner"))
    return client


def detail_url(booking):
    return reverse("notifications:booking_detail", args=[booking.pk])


def test_detail_uses_actual_amounts_arabic_dates_and_complete_form_actions(owner_client):
    booking = confirmed_reservation()
    payment(booking, amount=booking.total_price)
    with (
        patch("httpx.Client.send", side_effect=AssertionError("No external access on GET")),
        translation.override("en"),
    ):
        response = owner_client.get(detail_url(booking))
    assert response.status_code == 200
    html = response.content.decode()
    nodes = PageElements(html).elements
    panels = [attrs for _, attrs in nodes if "data-booking-panel" in attrs]
    assert {attrs["data-booking-panel"] for attrs in panels} == {
        "overview",
        "invoice",
        "payment",
        "edit",
    }
    assert all("hidden" not in attrs for attrs in panels)  # No-JS fallback.
    assert html.count("<h1>") == 1
    assert "admin-booking-detail.css" in html
    assert "admin-booking-detail.js" in html
    assert "luxury-living-lockup-dark.svg" in html
    assert "إجمالي المدفوع الموثق" in html
    assert "متوسط سعر الليلة" in html
    assert "لغة الضيف" in html
    assert "New check-in date" not in html
    assert "Number of guests" not in html
    assert "money--en" not in html
    actions = {
        attrs.get("value")
        for tag, attrs in nodes
        if tag == "input" and attrs.get("name") == "action"
    }
    assert actions >= {
        "send_guest_payment_link",
        "request_accounting_payment_link",
        "record_offline_payment",
        "quote_change",
        "start_cancellation",
    }
    assert b'data-default-panel="overview"' in response.content
    assert response.context["reservation"].admin_paid_totals[0].amount == booking.total_price
    assert not OfflinePaymentReceipt.objects.exists()
    assert not GuestInvoiceDelivery.objects.exists()


def test_pending_adjustment_reopens_edit_and_keeps_final_price_form(owner_client):
    booking = confirmed_reservation()
    adjustment = create_extension(booking).request
    assert adjustment is not None
    response = owner_client.get(detail_url(booking))
    html = response.content.decode()
    assert response.context["detail_default_panel"] == "edit"
    assert 'value="set_final_price"' in html
    assert str(adjustment.pk) in html
    assert 'value="quote_change"' in html


@pytest.mark.parametrize("section", ["overview", "invoice", "payment", "edit"])
def test_explicit_sections_are_allowlisted(owner_client, section):
    response = owner_client.get(detail_url(confirmed_reservation()), {"section": section})
    assert response.context["detail_default_panel"] == section


def test_invalid_section_is_never_used_as_markup(owner_client):
    response = owner_client.get(detail_url(confirmed_reservation()), {"section": "<script>"})
    assert response.context["detail_default_panel"] == "overview"
    assert "<script>" not in response.content.decode()


def test_invalid_date_post_stays_on_edit_with_errors_and_no_provider_write(owner_client):
    booking = confirmed_reservation()
    before = (booking.check_in, booking.check_out, booking.total_price)
    with patch("apps.reservations.operations_views.ModificationService") as provider:
        response = owner_client.post(
            detail_url(booking),
            {
                "action": "quote_change",
                "new_check_in": "not-a-date",
                "new_check_out": "",
                "new_guests": "0",
            },
        )
    provider.assert_not_called()
    assert response.status_code == 200
    assert response.context["detail_default_panel"] == "edit"
    assert response.context["date_form"].errors
    assert 'class="errorlist"' in response.content.decode()
    booking.refresh_from_db()
    assert (booking.check_in, booking.check_out, booking.total_price) == before


def test_invalid_receipt_post_stays_on_payment_with_confirmation_and_no_collection(owner_client):
    booking = confirmed_reservation()
    response = owner_client.post(
        detail_url(booking),
        {
            "action": "record_offline_payment",
            "amount": booking.total_price,
            "currency": booking.currency,
            "snapshot_digest": receipt_snapshot(booking),
            "method": "cash",
            "reference": "",
            "received_at": "bad-date",
            # Deliberately no receipt confirmation.
        },
    )
    assert response.status_code == 200
    assert response.context["detail_default_panel"] == "payment"
    assert "confirmed" in response.context["offline_receipt_form"].errors
    assert "received_at" in response.context["offline_receipt_form"].errors
    assert "lsa-booking-detail__check" in response.content.decode()
    assert not OfflinePaymentReceipt.objects.exists()
    assert not PaymentAttempt.objects.exists()


def test_paid_stay_keeps_accounting_option_but_does_not_offer_duplicate_local_receipt(owner_client):
    booking = confirmed_reservation()
    booking.payment_status = "paid"
    booking.save(update_fields=["payment_status"])
    response = owner_client.get(detail_url(booking))
    html = response.content.decode()
    assert response.context["detail_payment_settled"] is True
    assert 'value="record_offline_payment"' not in html
    assert 'value="send_guest_payment_link"' not in html
    assert 'value="request_accounting_payment_link"' in html
    assert "الحجز مسدد بالفعل" in html


def test_completed_stay_retains_disabled_cancel_and_server_guard(owner_client):
    booking = confirmed_reservation()
    booking.check_in = timezone.localdate() - timedelta(days=5)
    booking.check_out = timezone.localdate() - timedelta(days=3)
    booking.nights = 2
    booking.save()
    response = owner_client.get(detail_url(booking))
    assert response.context["cancellation_blocked"]
    assert response.context["detail_status_label"] == "انتهت الإقامة"
    html = response.content.decode()
    assert "الإلغاء غير متاح" in html
    assert 'value="start_cancellation"' not in html
    with patch("apps.reservations.operations_views.ModificationService") as provider:
        denied = owner_client.post(detail_url(booking), {"action": "start_cancellation"})
    assert denied.status_code == 200
    provider.assert_not_called()
    booking.refresh_from_db()
    assert booking.normalized_status == Reservation.Status.CONFIRMED


def test_anonymous_detail_is_still_protected():
    response = Client().get(detail_url(confirmed_reservation()))
    assert response.status_code == 302
