"""Owner navigation uses synthetic invoices; no provider writes or actual sends."""

from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from apps.integrations.hostaway.exceptions import HostawayTimeoutError
from apps.notifications.models import GuestInvoice, GuestInvoiceDelivery
from apps.notifications.services.invoices import issue_invoice
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.reservations.services.hostaway_booking import prepare_local_reservation
from tests import test_guest_invoices as invoice_fixtures
from tests.test_guest_invoices import reviewed
from tests.test_hostaway_booking_phase5 import make_intent

pytestmark = pytest.mark.django_db


@pytest.fixture
def actor():
    return invoice_fixtures.actor.__wrapped__()


@pytest.fixture
def property_obj():
    return invoice_fixtures.property_obj.__wrapped__()


@pytest.fixture
def payload(property_obj):
    return invoice_fixtures.payload.__wrapped__(property_obj)


@pytest.fixture
def draft(payload, actor):
    return invoice_fixtures.draft.__wrapped__(payload, actor)


def booking(invoice=None):
    property_obj = Property.objects.filter(hostaway_listing_id=8001).first()
    reservation = prepare_local_reservation(make_intent(property_obj=property_obj))
    reservation.normalized_status = Reservation.Status.CONFIRMED
    reservation.hostaway_reservation_id = invoice.hostaway_reservation_id if invoice else 77001
    reservation.save()
    return reservation


def test_booking_invoice_get_is_read_only_and_shows_lookup(client, actor):
    reservation = booking()
    client.force_login(actor)
    with patch("apps.notifications.invoice_views.HostawayClient") as provider:
        response = client.get(reverse("notifications:booking_detail", args=[reservation.pk]))
    assert response.status_code == 200
    assert "إعداد واستعراض فاتورة الحجز" in response.content.decode()
    assert response.content.decode().count("<h1>") == 1
    provider.assert_not_called()
    assert not GuestInvoice.objects.exists()
    assert not GuestInvoiceDelivery.objects.exists()


def test_booking_shows_issued_invoice_send_buttons_without_sending(client, actor, draft):
    invoice = issue_invoice(draft.pk, reviewed(draft))
    reservation = booking(invoice)
    client.force_login(actor)
    response = client.get(reverse("notifications:booking_detail", args=[reservation.pk]))
    html = response.content.decode()
    assert response.status_code == 200
    assert reverse("notifications:invoice_detail", args=[invoice.pk]) in html
    assert "إرسال الفاتورة عبر واتساب" in html
    assert "إرسال الفاتورة عبر البريد" in html
    assert html.count('name="confirm_recipient"') == 2
    assert not GuestInvoiceDelivery.objects.exists()


def test_booking_invoice_reuses_issued_invoice_without_provider_read(client, actor, draft):
    invoice = issue_invoice(draft.pk, reviewed(draft))
    reservation = booking(invoice)
    client.force_login(actor)
    with patch("apps.notifications.invoice_views.HostawayClient") as provider:
        response = client.post(reverse("notifications:booking_invoice", args=[reservation.pk]))
    assert response.url == reverse("notifications:invoice_detail", args=[invoice.pk])
    provider.assert_not_called()
    assert GuestInvoice.objects.count() == 1
    assert not GuestInvoiceDelivery.objects.exists()


def test_explicit_booking_invoice_lookup_prepares_only_draft(client, actor, payload):
    reservation = booking()
    reservation.hostaway_reservation_id = payload["result"]["id"]
    reservation.save(update_fields=["hostaway_reservation_id"])
    client.force_login(actor)
    with patch("apps.notifications.invoice_views.HostawayClient") as provider:
        api = provider.return_value.__enter__.return_value
        api.get_reservation_invoice_data.return_value = payload
        response = client.post(reverse("notifications:booking_invoice", args=[reservation.pk]))
        api.get_reservation_invoice_data.assert_called_once_with(
            reservation.hostaway_reservation_id
        )
        api.cancel_reservation.assert_not_called()
        api.update_reservation.assert_not_called()
    assert response.status_code == 302
    invoice = GuestInvoice.objects.get()
    assert invoice.issued_at is None
    assert not GuestInvoiceDelivery.objects.exists()


def test_booking_invoice_refreshes_unissued_draft_explicitly(client, actor, draft, payload):
    reservation = booking(draft)
    client.force_login(actor)
    path = reverse("notifications:booking_invoice", args=[reservation.pk])
    with patch("apps.notifications.invoice_views.HostawayClient") as provider:
        html = client.get(
            reverse("notifications:booking_detail", args=[reservation.pk])
        ).content.decode()
        assert "تحديث المسودة من الحجز" in html
        provider.assert_not_called()
        payload["result"]["totalPrice"] = "1900.00"
        payload["result"]["departureDate"] = "2026-12-18"
        api = provider.return_value.__enter__.return_value
        api.get_reservation_invoice_data.return_value = payload
        response = client.post(path)
        api.get_reservation_invoice_data.assert_called_once_with(draft.hostaway_reservation_id)
        api.update_reservation.assert_not_called()
        api.cancel_reservation.assert_not_called()
    assert response.url == reverse("notifications:invoice_detail", args=[draft.pk])
    draft.refresh_from_db()
    assert draft.snapshot["total"] == "1900.00"
    assert draft.snapshot["check_out"] == "2026-12-18"
    assert draft.issued_at is None
    assert GuestInvoice.objects.count() == 1
    assert not GuestInvoiceDelivery.objects.exists()


def test_booking_invoice_missing_id_and_timeout_do_not_issue(client, actor):
    reservation = booking()
    client.force_login(actor)
    with patch("apps.notifications.invoice_views.HostawayClient") as provider:
        provider.return_value.__enter__.return_value.get_reservation_invoice_data.side_effect = (
            HostawayTimeoutError("synthetic timeout")
        )
        response = client.post(reverse("notifications:booking_invoice", args=[reservation.pk]))
    assert response.url == reverse("notifications:booking_detail", args=[reservation.pk])
    assert not GuestInvoice.objects.exists()
    reservation.hostaway_reservation_id = None
    reservation.normalized_status = Reservation.Status.CREATE_FAILED
    reservation.save(update_fields=["hostaway_reservation_id", "normalized_status"])
    with patch("apps.notifications.invoice_views.HostawayClient") as provider:
        client.post(reverse("notifications:booking_invoice", args=[reservation.pk]))
        provider.assert_not_called()


def test_booking_invoice_endpoint_requires_owner_and_post(actor):
    reservation = booking()
    client = Client()
    path = reverse("notifications:booking_invoice", args=[reservation.pk])
    with patch("apps.notifications.invoice_views.HostawayClient") as provider:
        assert client.post(path).status_code == 302
        client.force_login(actor)
        assert client.get(path).status_code == 405
        provider.assert_not_called()
