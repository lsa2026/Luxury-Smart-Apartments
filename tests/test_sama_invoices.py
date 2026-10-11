"""Real invoice services with synthetic reservations and mocked transport only."""

# ruff: noqa: F811
import hashlib
from unittest.mock import patch

import pytest
from django.test import Client, override_settings

from apps.notifications.models import GuestInvoice, GuestInvoiceDelivery
from apps.reservations import sama_invoice_api as api
from tests.test_guest_invoices import payload, property_obj  # noqa: F401

pytestmark = pytest.mark.django_db
TOKEN = "ab" * 32
PHONE = "+16462817246"
HEADERS = {"HTTP_AUTHORIZATION": "Bearer " + TOKEN}
ENABLED = override_settings(
    SAMA_BOOKING_ENABLED=True,
    SAMA_INVOICES_ENABLED=True,
    SAMA_BOOKING_KEY_SHA256=hashlib.sha256(TOKEN.encode()).hexdigest(),
    EMAIL_DELIVERY_ENABLED=True,
    ULTRAMSG_ENABLED=True,
)


def post(suffix, data, **headers):
    return Client(enforce_csrf_checks=True).post(
        "/reservations/sama/invoice-" + suffix + "/",
        data=data,
        content_type="application/json",
        secure=True,
        **(headers or HEADERS),
    )


def request_data(phone=PHONE, channels=None, **changes):
    return {
        "phone": phone,
        "selection": api.token(phone, reservation_id=88001234),
        "channels": channels or ["whatsapp"],
        "email": "",
        "language": "ar",
        "confirmed": True,
        **changes,
    }


@ENABLED
def test_read_options_is_phone_bound_no_invoice_or_guest_contact_disclosure(payload):
    with patch.object(api, "HostawayClient") as factory:
        provider = factory.return_value.__enter__.return_value
        provider.invoice_search_page.return_value = [payload["result"]]
        value = post("options", {"phone": PHONE, "cursor": None}).json()
    assert value["code"] == "invoice_options" and len(value["options"]) == 1
    assert value["match_key"] == api.phone_digest(PHONE)
    assert "guest@example.invalid" not in str(value) and "ضيف تجريبي" not in str(value)
    assert "must-not-persist" not in str(value)
    assert GuestInvoice.objects.count() == GuestInvoiceDelivery.objects.count() == 0


@ENABLED
def test_issue_and_queue_both_channels_once_with_actual_pdf(payload):
    with (
        patch.object(api, "HostawayClient") as factory,
        patch("apps.notifications.services.invoices.dispatch_invoice_delivery") as dispatch,
    ):
        factory.return_value.__enter__.return_value.get_reservation_invoice_data.return_value = (
            payload
        )
        data = request_data(channels=["email", "whatsapp"], email="chosen@example.invalid")
        first = post("send", data)
        second = post("send", data)
    assert first.status_code == 200 and first.json()["code"] == "invoice_requested", first.json()
    assert first.json() == second.json()
    invoice = GuestInvoice.objects.get()
    assert bytes(invoice.pdf_content).startswith(b"%PDF")
    assert invoice.snapshot["total"] == "1400.00"
    assert invoice.snapshot["guest_email"] == "guest@example.invalid"
    assert GuestInvoiceDelivery.objects.count() == 2
    assert set(GuestInvoiceDelivery.objects.values_list("recipient", flat=True)) == {
        PHONE,
        "chosen@example.invalid",
    }
    assert not invoice.created_by.is_staff and not invoice.created_by.is_superuser
    assert not invoice.created_by.has_usable_password()
    dispatch.assert_not_called()  # Test transaction never commits transport callbacks.


@ENABLED
@pytest.mark.parametrize(
    "changes",
    [
        {"phone": "+966501234567"},
        {"confirmed": False},
        {"channels": ["sms"]},
        {"channels": ["email"], "email": "bad"},
        {"channels": ["whatsapp", "whatsapp"]},
        {"selection": "tampered"},
        {"phone": "local"},
    ],
)
def test_invalid_or_cross_guest_requests_never_issue(payload, changes):
    with patch.object(api, "HostawayClient") as factory:
        value = post("send", {**request_data(), **changes})
    assert value.status_code in {400, 403, 409}
    factory.assert_not_called()
    assert GuestInvoice.objects.count() == GuestInvoiceDelivery.objects.count() == 0


@ENABLED
def test_fresh_booking_phone_and_amount_are_checked_before_send(payload):
    with patch.object(api, "HostawayClient") as factory:
        provider = factory.return_value.__enter__.return_value
        provider.get_reservation_invoice_data.return_value = {
            **payload,
            "result": {**payload["result"], "phone": "+966501234567"},
        }
        assert post("send", request_data()).status_code == 403
        assert GuestInvoice.objects.count() == 0
        provider.get_reservation_invoice_data.return_value = payload
        assert post("send", request_data()).json()["code"] == "invoice_requested"
        original = bytes(GuestInvoice.objects.get().pdf_content)
        provider.get_reservation_invoice_data.return_value = {
            **payload,
            "result": {**payload["result"], "totalPrice": "1500.00"},
        }
        assert post("send", request_data()).json()["code"] == "invoice_changed_review_required"
        assert bytes(GuestInvoice.objects.get().pdf_content) == original
        assert GuestInvoiceDelivery.objects.count() == 1


@ENABLED
def test_missing_reference_or_name_cannot_be_invented(payload):
    with patch.object(api, "HostawayClient") as factory:
        factory.return_value.__enter__.return_value.get_reservation_invoice_data.return_value = {
            **payload,
            "result": {**payload["result"], "channelReservationId": "", "guestName": ""},
        }
        assert post("send", request_data()).json()["code"] == "invoice_details_incomplete"
    assert GuestInvoice.objects.count() == 0


@ENABLED
def test_failed_or_ambiguous_delivery_is_not_retried_by_guest_request(payload):
    with patch.object(api, "HostawayClient") as factory:
        factory.return_value.__enter__.return_value.get_reservation_invoice_data.return_value = (
            payload
        )
        first = post("send", request_data()).json()
        delivery = GuestInvoiceDelivery.objects.get()
        for state in ("failed", "review", "accepted"):
            GuestInvoiceDelivery.objects.filter(pk=delivery.pk).update(
                status=state, attempt_count=1
            )
            value = post("send", request_data()).json()
            assert value["deliveries"][0]["status"] == state
            status = post("status", {"phone": PHONE, "deliveries": [str(delivery.pk)]})
            assert status.json()["deliveries"][0]["status"] == state
            assert (
                post(
                    "status", {"phone": "+966501234567", "deliveries": [str(delivery.pk)]}
                ).status_code
                == 403
            )
        assert len(first["deliveries"]) == GuestInvoiceDelivery.objects.count() == 1


def test_default_disabled_and_bearer_required():
    assert post("options", {"phone": PHONE, "cursor": None}).status_code == 503
    with ENABLED:
        assert (
            post(
                "options",
                {"phone": PHONE, "cursor": None},
                HTTP_AUTHORIZATION="Bearer " + "cd" * 32,
            ).status_code
            == 401
        )


@ENABLED
def test_expired_selection_and_disabled_channel_do_not_issue(payload):
    with patch("django.core.signing.time.time", return_value=1):
        expired = api.token(PHONE, reservation_id=88001234)
    with patch.object(api, "HostawayClient") as factory:
        assert post("send", request_data(selection=expired)).status_code == 409
        with override_settings(EMAIL_DELIVERY_ENABLED=False):
            assert (
                post(
                    "send",
                    request_data(channels=["email", "whatsapp"], email="guest@example.invalid"),
                ).json()["code"]
                == "delivery_disabled"
            )
        factory.assert_not_called()
    assert GuestInvoice.objects.count() == GuestInvoiceDelivery.objects.count() == 0


@ENABLED
@pytest.mark.django_db(transaction=True)
def test_concurrent_guest_requests_issue_and_queue_only_once(payload):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections, connection

    if connection.vendor != "postgresql":
        pytest.skip("Independent session row locks are checked on PostgreSQL CI.")
    api.actor()

    def send_one(_):
        close_old_connections()
        try:
            return post("send", request_data()).json()
        finally:
            close_old_connections()

    with (
        patch.object(api, "HostawayClient") as factory,
        patch("apps.notifications.services.invoices.dispatch_invoice_delivery") as dispatch,
    ):
        factory.return_value.__enter__.return_value.get_reservation_invoice_data.return_value = (
            payload
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send_one, [1, 2]))
    assert all(r["code"] == "invoice_requested" for r in results), results
    assert GuestInvoice.objects.count() == GuestInvoiceDelivery.objects.count() == 1
    assert results[0]["deliveries"][0]["id"] == results[1]["deliveries"][0]["id"]
    dispatch.assert_called_once()
