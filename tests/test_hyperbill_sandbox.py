"""Real contracts with synthetic HTTP and WhatsApp; never call providers in tests."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.notifications.services.ultramsg import UltraMsgConfigurationError, UltraMsgConnectionError
from apps.payments.hyperbill import (
    HyperBillClient,
    HyperBillError,
    create_guest_payment_link,
    reconcile_invoice,
    send_guest_payment_link,
)
from apps.payments.models import HyperBillInvoice, HyperBillWebhookSignal, PaymentAttempt
from apps.reservations.models import Reservation
from tests.test_hostaway_booking_phase5 import make_intent

pytestmark = pytest.mark.django_db

UAT = {
    "HYPERBILL_ENABLED": True,
    "HYPERPAY_ENVIRONMENT": "test",
    "HYPERBILL_BASE_URL": "https://hyperbill-sandbox.hyperpay.com",
    "HYPERBILL_EMAIL": "synthetic@example.invalid",
    "HYPERBILL_PASSWORD": "synthetic-password",
    "SITE_BASE_URL": "https://checkout-uat-v2.onrender.com",
    "HYPERBILL_WEBHOOK_SECRET": "test-only-opaque-callback-value-123456",
    "HYPERBILL_WHATSAPP_ALLOWED_NUMBERS": ["+16462817246"],
}


@pytest.fixture(autouse=True)
def sandbox_settings():
    cache.clear()
    with override_settings(**UAT):
        yield


def reservation():
    intent = make_intent()
    intent.guest_phone = "+16462817246"
    intent.expires_at = timezone.now() + timedelta(hours=2)
    intent.save()
    return Reservation.objects.create(
        booking_intent=intent,
        property=intent.property,
        hostaway_reservation_id=88001,
        normalized_status="confirmed",
        payment_status="unpaid",
        currency=intent.currency,
        total_price=intent.total_price,
        check_in=intent.check_in,
        check_out=intent.check_out,
        nights=intent.nights,
        guests=intent.guests,
    )


def document(invoice, **changes):
    data = {
        "invoice_no": "a" * 32,
        "merchant_invoice_number": invoice.merchant_reference,
        "amount": str(invoice.amount),
        "currency": "SAR",
        "payment_type": "DB",
        "status": "pending",
    }
    data.update(changes)
    return {"status": True, "data": [data], "url": "https://untrusted.invalid/link"}


def fake_create(payload):
    invoice = HyperBillInvoice.objects.get(merchant_reference=payload["merchant_invoice_number"])
    assert payload["phone"] == "16462817246"
    assert payload["amount"] == "500.25"
    return document(invoice)


def test_create_and_guest_whatsapp_are_idempotent_and_use_final_sar_price():
    booking = reservation()
    with (
        patch("apps.payments.hyperbill.HyperBillClient") as api,
        patch("apps.payments.hyperbill.UltraMsgClient") as wa,
    ):
        api.return_value.__enter__.return_value.create_invoice.side_effect = fake_create
        wa.return_value.__enter__.return_value.send_text.return_value = {"sent": True, "id": 42}
        assert create_guest_payment_link(reservation_id=booking.pk).code == "sent"
        assert create_guest_payment_link(reservation_id=booking.pk).code == "already_sent"
        assert api.return_value.__enter__.return_value.create_invoice.call_count == 1
        assert wa.return_value.__enter__.return_value.send_text.call_count == 1
        args = wa.return_value.__enter__.return_value.send_text.call_args.kwargs
        assert args["recipient"] == "+16462817246"
        assert "hyperbill-sandbox.hyperpay.com/invoice/show/simple/" in args["body"]
        assert "untrusted.invalid" not in args["body"]
    assert HyperBillInvoice.objects.count() == 1


def test_timeout_never_repeats_creation_and_recovers_by_reference():
    booking = reservation()
    with patch("apps.payments.hyperbill.HyperBillClient") as api:
        api.return_value.__enter__.return_value.create_invoice.side_effect = HyperBillError(
            "hyperbill_outcome_unknown"
        )
        assert (
            create_guest_payment_link(reservation_id=booking.pk).code == "hyperbill_outcome_unknown"
        )
        assert (
            create_guest_payment_link(reservation_id=booking.pk).code
            == "invoice_requires_reconciliation"
        )
        assert api.return_value.__enter__.return_value.create_invoice.call_count == 1
    invoice = HyperBillInvoice.objects.get()
    client = MagicMock()
    client.retrieve_reference.return_value = document(invoice)
    assert reconcile_invoice(invoice.pk, client=client).code == "pending"
    client.retrieve_reference.assert_called_once_with(invoice.merchant_reference)
    client.retrieve_invoice.assert_not_called()


def test_whatsapp_ambiguous_send_is_not_retried():
    booking = reservation()
    with (
        patch("apps.payments.hyperbill.HyperBillClient") as api,
        patch("apps.payments.hyperbill.UltraMsgClient") as wa,
    ):
        api.return_value.__enter__.return_value.create_invoice.side_effect = fake_create
        wa.return_value.__enter__.return_value.send_text.side_effect = UltraMsgConnectionError()
        assert (
            create_guest_payment_link(reservation_id=booking.pk).code
            == "guest_whatsapp_requires_review"
        )
        invoice = HyperBillInvoice.objects.get()
        assert send_guest_payment_link(invoice.pk).code == "delivery_requires_review"
        assert wa.return_value.__enter__.return_value.send_text.call_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": "1.00"},
        {"currency": "USD"},
        {"merchant_invoice_number": "wrong"},
        {"payment_type": "PA"},
        {"status": "unknown"},
        {"amount": "NaN"},
    ],
)
def test_unmatched_or_unknown_status_never_becomes_paid(changes):
    booking = reservation()
    invoice = HyperBillInvoice.objects.create(
        reservation=booking,
        merchant_reference="HBtest",
        amount="500.25",
        invoice_no="a" * 32,
        expires_at=timezone.now() + timedelta(hours=1),
        status="pending",
    )
    api = MagicMock()
    api.retrieve_invoice.return_value = document(invoice, **changes)
    assert reconcile_invoice(invoice.pk, client=api).code.startswith("hyperbill_")
    invoice.refresh_from_db()
    assert invoice.status == "pending"
    assert invoice.verified_at is None


def test_verified_paid_is_monotonic_but_never_collects_real_money_or_changes_hostaway():
    booking = reservation()
    invoice = HyperBillInvoice.objects.create(
        reservation=booking,
        merchant_reference="HBtest",
        amount="500.25",
        invoice_no="a" * 32,
        expires_at=timezone.now() + timedelta(hours=1),
        status="pending",
    )
    api = MagicMock()
    api.retrieve_invoice.return_value = document(invoice, status="paid")
    assert reconcile_invoice(invoice.pk, client=api).code == "paid"
    assert reconcile_invoice(invoice.pk, client=api).code == "paid"
    assert api.retrieve_invoice.call_count == 1
    booking.refresh_from_db()
    assert booking.payment_status == "unpaid"
    assert not PaymentAttempt.objects.exists()
    invoice.refresh_from_db()
    assert invoice.verified_at


@pytest.mark.parametrize(
    "field,value",
    [
        ("HYPERPAY_ENVIRONMENT", "production"),
        ("SITE_BASE_URL", "https://luxurysmartapartments.com"),
        ("HYPERBILL_ENABLED", False),
        ("HYPERBILL_BASE_URL", "https://hyperbill.hyperpay.com"),
    ],
)
def test_integration_refuses_production(field, value):
    with override_settings(**{field: value}), pytest.raises(HyperBillError):
        HyperBillClient()


def test_non_allowlisted_guest_and_cancelled_booking_are_blocked_before_invoice():
    booking = reservation()
    with override_settings(HYPERBILL_WHATSAPP_ALLOWED_NUMBERS=[]):
        assert (
            create_guest_payment_link(reservation_id=booking.pk).code
            == "sandbox_guest_not_allowlisted"
        )
    booking.normalized_status = "cancelled"
    booking.save()
    assert (
        create_guest_payment_link(reservation_id=booking.pk).code
        == "confirmed_unpaid_hostaway_booking_required"
    )
    assert not HyperBillInvoice.objects.exists()


def test_webhook_accepts_empty_post_without_trusting_a_paid_body(client):
    url = reverse("payments:hyperbill_webhook", args=[UAT["HYPERBILL_WEBHOOK_SECRET"]])
    with patch("apps.payments.hyperbill_views.reconcile_hyperbill_task.delay") as dispatch:
        assert client.post(url, data=b"", content_type="application/json").status_code == 200
        assert (
            client.post(
                url, data=json.dumps({"status": "paid"}), content_type="application/json"
            ).status_code
            == 200
        )
        dispatch.assert_not_called()
    assert HyperBillWebhookSignal.objects.count() == 1
    assert not PaymentAttempt.objects.exists()
    assert client.get(url).status_code == 405
    assert client.post(reverse("payments:hyperbill_webhook", args=["wrong"])).status_code == 404


def test_webhook_queue_failure_keeps_durable_signal_and_returns_200(client):
    with (
        override_settings(HYPERBILL_RECONCILIATION_ENABLED=True),
        patch(
            "apps.payments.hyperbill_views.reconcile_hyperbill_task.delay",
            side_effect=ConnectionError,
        ),
    ):
        url = reverse("payments:hyperbill_webhook", args=[UAT["HYPERBILL_WEBHOOK_SECRET"]])
        assert client.post(url, data=b"", content_type="application/json").status_code == 200
    assert HyperBillWebhookSignal.objects.filter(processed_at__isnull=True).count() == 1


def test_http_contract_and_auth_token_remain_server_side():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/login":
            assert request.headers.get("Authorization") is None
            assert json.loads(request.content)["email"] == UAT["HYPERBILL_EMAIL"]
            return httpx.Response(200, json={"status": True, "data": {"accessToken": "synthetic"}})
        assert request.headers["Authorization"] == "Bearer synthetic"
        return httpx.Response(200, json={"status": True, "data": []})

    with httpx.Client(
        base_url=UAT["HYPERBILL_BASE_URL"], transport=httpx.MockTransport(handler)
    ) as http:
        with HyperBillClient(http=http) as api:
            api.retrieve_reference("HB123")
            api.retrieve_invoice("a" * 32)
    assert calls == [
        "/api/login",
        "/api/simpleInvoice/retrieve/min/HB123",
        "/api/simpleInvoice/retrieve/" + "a" * 32,
    ]


@pytest.mark.parametrize(
    "status,body", [(401, {"status": False}), (500, {}), (200, {"status": False}), (302, {})]
)
def test_api_failures_are_sanitized(status, body):
    def handler(request):
        return httpx.Response(status, json={**body, "sensitive": "MUST_NOT_LEAK"})

    with httpx.Client(
        base_url=UAT["HYPERBILL_BASE_URL"], transport=httpx.MockTransport(handler)
    ) as http:
        with HyperBillClient(http=http) as api, pytest.raises(HyperBillError) as error:
            api.retrieve_reference("HB123")
    assert "MUST_NOT_LEAK" not in str(error.value)
    assert "synthetic-password" not in str(error.value)


def test_owner_action_uses_guest_link_and_returns_to_bookings_without_accounting(client):
    from types import SimpleNamespace

    from apps.notifications.models import AuditLog
    from apps.reservations.manual_bookings import ManualBookingHostawayCreation
    from apps.reservations.models import ManualBookingDraft
    from tests.test_manual_booking_operations_phase61 import owner

    booking = reservation()
    intent = booking.booking_intent
    draft = ManualBookingDraft.objects.create(
        quote=intent.quote,
        property=intent.property,
        check_in=intent.check_in,
        check_out=intent.check_out,
        nights=intent.nights,
        guests=intent.guests,
        currency=intent.currency,
        system_total_price=intent.total_price,
        final_total_price=intent.total_price,
        status="ready_for_payment",
        availability_checked_at=timezone.now(),
        expires_at=intent.expires_at,
    )
    client.force_login(owner())
    with (
        patch(
            "apps.reservations.operations_views.create_manual_booking_in_hostaway",
            return_value=ManualBookingHostawayCreation("already_created", draft, booking),
        ),
        patch(
            "apps.payments.hyperbill.create_guest_payment_link",
            return_value=SimpleNamespace(code="sent"),
        ) as guest,
        patch("apps.reservations.operations_views.send_manual_payment_link_request") as aseel,
    ):
        response = client.post(
            reverse("notifications:manual_booking_detail", args=[draft.pk]),
            {"action": "create_hostaway_and_request_payment"},
        )
    assert response.status_code == 302
    assert response["Location"] == reverse("notifications:booking_list")
    guest.assert_called_once_with(reservation_id=booking.pk)
    aseel.assert_not_called()
    assert AuditLog.objects.filter(action="manual_booking.hyperbill_sandbox").exists()


def test_price_change_after_link_creation_is_not_silently_reused():
    booking = reservation()
    HyperBillInvoice.objects.create(
        reservation=booking,
        merchant_reference="HBold",
        amount="50.00",
        status="pending",
        expires_at=timezone.now() + timedelta(hours=1),
    )
    assert (
        create_guest_payment_link(reservation_id=booking.pk).code == "price_changed_requires_review"
    )


@pytest.mark.parametrize("changed", ["paid", "price", "expired"])
def test_manual_send_refuses_booking_that_is_no_longer_payable(changed):
    booking = reservation()
    invoice = HyperBillInvoice.objects.create(
        reservation=booking,
        merchant_reference="HBexisting",
        amount=booking.booking_intent.payment_amount_sar,
        status="pending",
        payment_url="https://hyperbill-sandbox.hyperpay.com/invoice/show/simple/" + "a" * 32,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    if changed == "paid":
        booking.payment_status = "paid"
        booking.save()
    elif changed == "price":
        booking.total_price += 1
        booking.save()
    else:
        booking.booking_intent.expires_at = timezone.now() - timedelta(minutes=1)
        booking.booking_intent.save()
    with patch("apps.payments.hyperbill.UltraMsgClient") as wa:
        assert send_guest_payment_link(invoice.pk).code == "invoice_not_payable"
    wa.assert_not_called()


def test_disabled_whatsapp_can_be_configured_then_sent_without_recreating_invoice():
    booking = reservation()
    with patch("apps.payments.hyperbill.HyperBillClient") as api:
        api.return_value.__enter__.return_value.create_invoice.side_effect = fake_create
        with patch(
            "apps.payments.hyperbill.UltraMsgClient", side_effect=UltraMsgConfigurationError
        ):
            result = create_guest_payment_link(reservation_id=booking.pk)
        assert result.code == "guest_whatsapp_configuration_missing"
        result.invoice.refresh_from_db()
        assert result.invoice.delivery_status == "not_sent"
        with patch("apps.payments.hyperbill.UltraMsgClient") as wa:
            wa.return_value.__enter__.return_value.send_text.return_value = {"id": "synthetic"}
            assert create_guest_payment_link(reservation_id=booking.pk).code == "sent"
        assert api.return_value.__enter__.return_value.create_invoice.call_count == 1


def test_owner_can_check_api_login_without_invoice_or_guest_message(client):
    from tests.test_manual_booking_operations_phase61 import owner

    client.force_login(owner())
    with patch("apps.payments.hyperbill.HyperBillClient") as api:
        response = client.post(
            reverse("notifications:booking_list"), {"action": "check_hyperbill_connection"}
        )
    assert response.status_code == 302
    api.return_value.__enter__.return_value.check_connection.assert_called_once()
    api.return_value.__enter__.return_value.create_invoice.assert_not_called()
    assert HyperBillInvoice.objects.count() == 0


def test_deployment_connection_check_never_creates_an_invoice():
    from io import StringIO

    from django.core.management import call_command

    output = StringIO()
    with patch(
        "apps.payments.management.commands.check_hyperbill_connection.HyperBillClient"
    ) as api:
        call_command("check_hyperbill_connection", stdout=output)
    api.return_value.__enter__.return_value.check_connection.assert_called_once()
    api.return_value.__enter__.return_value.create_invoice.assert_not_called()
    assert "HYPERBILL_SANDBOX_API_CONNECTED" in output.getvalue()
    assert HyperBillInvoice.objects.count() == 0
