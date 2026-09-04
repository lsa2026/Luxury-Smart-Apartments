from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.payments.checks import hyperpay_configuration_check
from apps.payments.hyperpay.client import HyperPayClient
from apps.payments.hyperpay.exceptions import (
    HyperPayCheckoutError,
    HyperPayConfigurationError,
    HyperPayConnectionError,
)
from apps.payments.hyperpay.result_codes import HyperPayStatus, map_result_code
from apps.payments.hyperpay.service import (
    CheckoutSession,
    HyperPayService,
    VerificationOutcome,
    build_checkout_payload,
    format_test_amount,
)
from apps.payments.models import PaymentAttempt
from apps.payments.views import (
    HyperPayBookingCheckoutView,
    HyperPayModificationCheckoutView,
    HyperPayResultView,
)
from apps.reservations.models import (
    BookingIntent,
    BookingModificationRequest,
    Reservation,
)
from apps.reservations.security import SESSION_MARKER_KEY, hash_session_marker
from apps.reservations.services.modifications import (
    ModificationRevalidation,
    ModificationService,
)
from tests.test_booking_models_services import make_availability
from tests.test_booking_modifications_phase6 import (
    ModificationAvailabilityStub,
    confirmed_reservation,
    create_extension,
)
from tests.test_hostaway_booking_phase5 import (
    AvailabilityStub,
    complete_availability,
    make_intent,
)

pytestmark = pytest.mark.django_db

HYPERPAY_SETTINGS = {
    "HYPERPAY_ENABLED": True,
    "HYPERPAY_ENVIRONMENT": "test",
    "HYPERPAY_BASE_URL": "https://eu-test.oppwa.com/",
    "HYPERPAY_ENTITY_ID": "test-entity-id",
    "HYPERPAY_ACCESS_TOKEN": "test-access-token-secret",
    "HYPERPAY_CURRENCY": "SAR",
    "HYPERPAY_PAYMENT_TYPE": "DB",
    "HYPERPAY_ALLOWED_BRANDS": ("MADA", "VISA", "MASTER"),
    "HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED": False,
}


class HyperPayStub:
    def __init__(self) -> None:
        self.checkout_calls = 0
        self.payment_calls = 0
        self.payload = None
        self.payment_document = None

    def create_checkout(self, payload):
        self.checkout_calls += 1
        self.payload = payload
        return {
            "id": "ABC123456789.uat01-vm-tx04",
            "integrity": "sha384-YWJj",
            "result": {"code": "000.200.100"},
        }

    def get_checkout_payment(self, checkout_id):
        assert checkout_id == "ABC123456789.uat01-vm-tx04"
        self.payment_calls += 1
        return self.payment_document

    def close(self) -> None:
        pass


def payable_intent():
    intent = make_intent()
    intent.total_price = Decimal("1250.0000")
    intent.billing_street1 = "King Fahd Road 10"
    intent.billing_city = "Riyadh"
    intent.billing_state = "Riyadh"
    intent.billing_country = "SA"
    intent.billing_postcode = "12345"
    intent.save()
    return intent


def own_intent(client: Client, intent) -> None:
    marker = "hyperpay-owner-marker"
    session = client.session
    session[SESSION_MARKER_KEY] = marker
    session.save()
    intent.session_key_hash = hash_session_marker(marker)
    intent.save(update_fields=["session_key_hash"])


@override_settings(**HYPERPAY_SETTINGS)
def test_checkout_payload_uses_trusted_required_test_values() -> None:
    intent = payable_intent()
    payload = build_checkout_payload(intent, "LSA-1234567890")
    assert payload == {
        "entityId": "test-entity-id",
        "amount": "1250.00",
        "currency": "SAR",
        "paymentType": "DB",
        "merchantTransactionId": "LSA-1234567890",
        "testMode": "EXTERNAL",
        "customParameters[3DS2_enrolled]": "true",
        "integrity": "true",
        "billing.country": "SA",
        "customer.email": intent.guest_email,
        "customer.givenName": intent.guest_first_name,
        "customer.surname": intent.guest_last_name,
        "billing.street1": intent.billing_street1,
        "billing.city": intent.billing_city,
        "billing.state": intent.billing_state,
        "billing.postcode": intent.billing_postcode,
    }


def test_test_amount_requires_whole_sar_and_formats_two_decimals() -> None:
    assert format_test_amount(Decimal("1250")) == "1250.00"
    with pytest.raises(ValueError, match="whole_sar"):
        format_test_amount(Decimal("1250.25"))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("000.000.000", HyperPayStatus.SUCCESS),
        ("000.100.110", HyperPayStatus.SUCCESS),
        ("000.200.100", HyperPayStatus.PENDING),
        ("000.400.010", HyperPayStatus.REVIEW),
        ("100.396.101", HyperPayStatus.CANCELLED),
        ("800.100.153", HyperPayStatus.FAILED),
        ("unexpected", HyperPayStatus.UNKNOWN),
    ],
)
def test_result_code_mapping(code: str, expected: HyperPayStatus) -> None:
    assert map_result_code(code) is expected


@override_settings(**HYPERPAY_SETTINGS)
def test_checkout_creation_persists_unique_traceable_identifiers() -> None:
    intent = payable_intent()
    client = HyperPayStub()
    session = HyperPayService(client=client).create_checkout(intent)
    second = HyperPayService(client=client).create_checkout(intent)
    assert session.attempt.pk == second.attempt.pk
    assert client.checkout_calls == 1
    assert session.attempt.merchant_transaction_id.startswith("LSA-")
    assert session.attempt.provider_checkout_id == "ABC123456789.uat01-vm-tx04"
    assert PaymentAttempt.objects.count() == 1
    assert client.payload["amount"] == "1250.00"


@override_settings(
    **(HYPERPAY_SETTINGS | {"HOSTAWAY_LIVE_BOOKING_ENABLED": True})
)
def test_checkout_refuses_to_charge_without_verified_listing_map_id() -> None:
    intent = payable_intent()
    intent.property.hostaway_listing_map_id = None
    intent.property.save(update_fields=["hostaway_listing_map_id"])
    client = HyperPayStub()

    with pytest.raises(HyperPayCheckoutError, match="listing_map_id_not_verified"):
        HyperPayService(client=client).create_checkout(intent)

    assert client.checkout_calls == 0
    assert PaymentAttempt.objects.count() == 0


@override_settings(
    **(HYPERPAY_SETTINGS | {"HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED": True})
)
def test_checkout_revalidates_live_inventory_immediately_before_payment() -> None:
    intent = payable_intent()
    availability = AvailabilityStub(complete_availability(intent))
    client = HyperPayStub()
    HyperPayService(client=client, availability_service=availability).create_checkout(intent)
    assert availability.calls == 1
    assert client.checkout_calls == 1


@override_settings(
    **(HYPERPAY_SETTINGS | {"HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED": True})
)
def test_unavailable_inventory_blocks_checkout_before_hyperpay() -> None:
    intent = payable_intent()
    unavailable = replace(
        make_availability(intent.property, available=False),
        nights=intent.nights,
    )
    client = HyperPayStub()
    with pytest.raises(HyperPayCheckoutError, match="prepayment_unavailable"):
        HyperPayService(
            client=client,
            availability_service=AvailabilityStub(unavailable),
        ).create_checkout(intent)
    intent.refresh_from_db()
    assert intent.status == BookingIntent.Status.UNAVAILABLE
    assert client.checkout_calls == 0
    assert PaymentAttempt.objects.count() == 0


@override_settings(
    **(HYPERPAY_SETTINGS | {"HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED": True})
)
def test_price_change_blocks_checkout_before_hyperpay() -> None:
    intent = payable_intent()
    changed = complete_availability(intent, total=Decimal("1300"))
    client = HyperPayStub()
    with pytest.raises(HyperPayCheckoutError, match="prepayment_price_changed"):
        HyperPayService(
            client=client,
            availability_service=AvailabilityStub(changed),
        ).create_checkout(intent)
    intent.refresh_from_db()
    assert intent.status == BookingIntent.Status.PRICE_CHANGED
    assert client.checkout_calls == 0


@override_settings(**HYPERPAY_SETTINGS)
def test_modification_difference_checkout_is_revalidated_and_sar_only() -> None:
    reservation = confirmed_reservation()
    availability = ModificationAvailabilityStub(reservation)
    modification = create_extension(reservation, stub=availability).request
    assert modification is not None
    client = HyperPayStub()
    service = HyperPayService(
        client=client,
        modification_service=ModificationService(availability_service=availability),
    )
    checkout = service.create_modification_checkout(modification)
    assert checkout.attempt.modification_request == modification
    assert checkout.attempt.amount == Decimal("150.0000")
    assert client.payload["amount"] == "150.00"
    assert client.payload["currency"] == "SAR"


@override_settings(**HYPERPAY_SETTINGS)
def test_paid_modification_is_revalidated_again_after_3ds() -> None:
    reservation = confirmed_reservation()
    availability = ModificationAvailabilityStub(reservation)
    modification = create_extension(reservation, stub=availability).request
    assert modification is not None
    client = HyperPayStub()
    service = HyperPayService(
        client=client,
        modification_service=ModificationService(availability_service=availability),
    )
    checkout = service.create_modification_checkout(modification)
    client.payment_document = {
        "id": "modification_payment_12345678",
        "amount": "150.00",
        "currency": "SAR",
        "paymentType": "DB",
        "paymentBrand": "MADA",
        "merchantTransactionId": checkout.attempt.merchant_transaction_id,
        "entityId": "test-entity-id",
        "result": {"code": "000.100.110", "description": "Test success"},
    }
    outcome = service.verify(checkout.attempt)
    modification.refresh_from_db()
    assert outcome.status is HyperPayStatus.SUCCESS
    assert outcome.modification == modification
    assert modification.status == BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
    assert availability.calendar_calls == 3
    assert availability.price_calls == 3


@override_settings(**HYPERPAY_SETTINGS)
def test_paid_modification_never_returns_to_pay_button_after_revalidation_error() -> None:
    reservation = confirmed_reservation()
    availability = ModificationAvailabilityStub(reservation)
    modification = create_extension(reservation, stub=availability).request
    assert modification is not None
    client = HyperPayStub()
    service = HyperPayService(
        client=client,
        modification_service=ModificationService(availability_service=availability),
    )
    checkout = service.create_modification_checkout(modification)

    class TemporarilyUnavailable:
        @staticmethod
        def revalidate_for_payment(item):
            assert item.pk == modification.pk
            return ModificationRevalidation("hostaway_temporarily_unavailable")

    service.modification_service = TemporarilyUnavailable()
    client.payment_document = {
        "id": "modification_payment_error_12345678",
        "amount": "150.00",
        "currency": "SAR",
        "paymentType": "DB",
        "paymentBrand": "VISA",
        "merchantTransactionId": checkout.attempt.merchant_transaction_id,
        "entityId": "test-entity-id",
        "result": {"code": "000.100.110", "description": "Test success"},
    }
    outcome = service.verify(checkout.attempt)
    modification.refresh_from_db()
    assert outcome.status is HyperPayStatus.SUCCESS
    assert modification.status == BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
    assert outcome.automatic_modification is not None
    assert outcome.automatic_modification.code == "postpayment_revalidation_failed"


@override_settings(**HYPERPAY_SETTINGS, HOSTAWAY_LIVE_BOOKING_ENABLED=False)
def test_verified_success_creates_one_local_reservation_without_duplicate() -> None:
    intent = payable_intent()
    client = HyperPayStub()
    checkout = HyperPayService(client=client).create_checkout(intent)
    client.payment_document = {
        "id": "payment_12345678",
        "amount": "1250.00",
        "currency": "SAR",
        "paymentType": "DB",
        "paymentBrand": "MADA",
        "merchantTransactionId": checkout.attempt.merchant_transaction_id,
        "entityId": "test-entity-id",
        "result": {"code": "000.100.110", "description": "Test success"},
    }
    first = HyperPayService(client=client).verify(checkout.attempt)
    second = HyperPayService(client=client).verify(checkout.attempt)
    checkout.attempt.refresh_from_db()
    intent.refresh_from_db()
    reservation = Reservation.objects.get(booking_intent=intent)
    assert first.status is HyperPayStatus.SUCCESS
    assert second.status is HyperPayStatus.SUCCESS
    assert checkout.attempt.status == PaymentAttempt.Status.SUCCEEDED
    assert checkout.attempt.verified_at is not None
    assert intent.status == BookingIntent.Status.PAYMENT_VERIFIED
    assert reservation.payment_status == "paid"
    assert reservation.normalized_status == Reservation.Status.READY_FOR_HOSTAWAY
    assert client.payment_calls == 1
    assert Reservation.objects.filter(booking_intent=intent).count() == 1


@pytest.mark.parametrize(
    ("field", "value", "failure_code"),
    [
        ("amount", "1251.00", "amount_mismatch"),
        ("currency", "USD", "currency_mismatch"),
        ("paymentType", "PA", "paymentType_mismatch"),
        ("merchantTransactionId", "LSA-tampered", "merchantTransactionId_mismatch"),
        ("paymentBrand", "AMEX", "payment_brand_mismatch"),
    ],
)
@override_settings(**HYPERPAY_SETTINGS)
def test_verification_mismatch_never_creates_reservation(
    field: str,
    value: str,
    failure_code: str,
) -> None:
    intent = payable_intent()
    client = HyperPayStub()
    checkout = HyperPayService(client=client).create_checkout(intent)
    document = {
        "id": "payment_12345678",
        "amount": "1250.00",
        "currency": "SAR",
        "paymentType": "DB",
        "paymentBrand": "VISA",
        "merchantTransactionId": checkout.attempt.merchant_transaction_id,
        "result": {"code": "000.000.000", "description": "Success"},
    }
    document[field] = value
    client.payment_document = document
    outcome = HyperPayService(client=client).verify(checkout.attempt)
    checkout.attempt.refresh_from_db()
    assert outcome.status is HyperPayStatus.REVIEW
    assert checkout.attempt.failure_code == failure_code
    assert Reservation.objects.count() == 0


@override_settings(**HYPERPAY_SETTINGS)
def test_http_client_uses_bearer_server_side_and_explicit_entity_id(caplog) -> None:
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"result": {"code": "000.200.100"}})

    http = httpx.Client(
        base_url="https://eu-test.oppwa.com/",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-access-token-secret"},
    )
    client = HyperPayClient(http=http)
    client.get_checkout_payment("checkout_12345678")
    assert observed[0].url.path == "/v1/checkouts/checkout_12345678/payment"
    assert observed[0].url.params["entityId"] == "test-entity-id"
    assert observed[0].headers["Authorization"] == "Bearer test-access-token-secret"
    assert "test-access-token-secret" not in caplog.text
    http.close()


@override_settings(**HYPERPAY_SETTINGS)
def test_http_timeout_is_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic", request=request)

    http = httpx.Client(
        base_url="https://eu-test.oppwa.com/",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HyperPayConnectionError):
        HyperPayClient(http=http).get_checkout_payment("checkout_12345678")
    http.close()


@override_settings(HYPERPAY_ENABLED=True, HYPERPAY_ENTITY_ID="", HYPERPAY_ACCESS_TOKEN="")
def test_missing_credentials_fail_safely() -> None:
    with pytest.raises(HyperPayConfigurationError):
        HyperPayClient()


PRODUCTION_AUTOMATION = {
    "HYPERPAY_ENABLED": True,
    "HYPERPAY_ENVIRONMENT": "production",
    "HYPERPAY_BASE_URL": "https://eu-prod.oppwa.com/",
    "HYPERPAY_ENTITY_ID": "production-like-entity",
    "HYPERPAY_ACCESS_TOKEN": "production-like-token",
    "HYPERPAY_CURRENCY": "SAR",
    "HYPERPAY_PAYMENT_TYPE": "DB",
    "HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED": True,
    "HOSTAWAY_LIVE_BOOKING_ENABLED": True,
    "BOOKING_AUTOMATIC_MODIFICATION_APPROVAL": True,
    "HOSTAWAY_LIVE_MODIFICATION_ENABLED": True,
    "HOSTAWAY_LIVE_EXTENSION_ENABLED": True,
    "BOOKING_AUTOMATIC_CANCELLATION_ENABLED": False,
    "HOSTAWAY_LIVE_CANCELLATION_ENABLED": False,
}


@override_settings(**PRODUCTION_AUTOMATION)
def test_the_shipped_automation_combination_boots() -> None:
    """These are the values render.yaml now carries for every service."""
    assert hyperpay_configuration_check() == []


@override_settings(**{**PRODUCTION_AUTOMATION, "HOSTAWAY_LIVE_EXTENSION_ENABLED": False})
def test_automatic_approval_without_live_extension_is_refused() -> None:
    """The three flags have to move together or a request would stall unapplied."""
    errors = hyperpay_configuration_check()

    assert {error.id for error in errors} >= {"payments.E108"}


@override_settings(
    **{**PRODUCTION_AUTOMATION, "BOOKING_AUTOMATIC_CANCELLATION_ENABLED": True}
)
def test_automatic_cancellation_without_live_cancellation_is_refused() -> None:
    errors = hyperpay_configuration_check()

    assert {error.id for error in errors} >= {"payments.E109"}


@override_settings(
    HYPERPAY_ENABLED=True,
    HYPERPAY_ENVIRONMENT="production",
    HYPERPAY_BASE_URL="https://eu-test.oppwa.com/",
    HYPERPAY_ENTITY_ID="production-like-entity",
    HYPERPAY_ACCESS_TOKEN="production-like-token",
)
def test_production_cannot_inherit_test_configuration() -> None:
    errors = hyperpay_configuration_check()
    assert {error.id for error in errors} >= {"payments.E102"}


@override_settings(
    **(
        HYPERPAY_SETTINGS
        | {
            "HYPERPAY_ENVIRONMENT": "production",
            "HYPERPAY_BASE_URL": "https://eu-prod.oppwa.com/",
            "HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED": False,
        }
    )
)
def test_production_requires_prepayment_revalidation() -> None:
    errors = hyperpay_configuration_check()
    assert {error.id for error in errors} >= {"payments.E106", "payments.E107"}


@override_settings(
    **HYPERPAY_SETTINGS,
    BOOKING_AUTOMATIC_MODIFICATION_APPROVAL=True,
    HOSTAWAY_LIVE_MODIFICATION_ENABLED=False,
    HOSTAWAY_LIVE_EXTENSION_ENABLED=False,
)
def test_automatic_modifications_require_both_hostaway_write_flags() -> None:
    errors = hyperpay_configuration_check()
    assert {error.id for error in errors} >= {"payments.E108"}


@override_settings(
    **(
        HYPERPAY_SETTINGS
        | {
            "HYPERPAY_ENVIRONMENT": "production",
            "HYPERPAY_BASE_URL": "https://eu-prod.oppwa.com/",
        }
    )
)
def test_production_payload_omits_test_only_parameters() -> None:
    payload = build_checkout_payload(payable_intent(), "LSA-production-123456")
    assert payload["amount"] == "1250.00"
    assert "testMode" not in payload
    assert "customParameters[3DS2_enrolled]" not in payload


@override_settings(**HYPERPAY_SETTINGS, HOSTAWAY_LIVE_BOOKING_ENABLED=True)
def test_test_payments_cannot_enable_live_hostaway_booking_writes() -> None:
    errors = hyperpay_configuration_check()
    assert {error.id for error in errors} >= {"payments.E105"}


@override_settings(
    **HYPERPAY_SETTINGS,
    HOSTAWAY_LIVE_MODIFICATION_ENABLED=True,
)
def test_test_payments_cannot_enable_live_hostaway_modification_writes() -> None:
    errors = hyperpay_configuration_check()
    assert {error.id for error in errors} >= {"payments.E105"}


class CheckoutViewServiceStub:
    checkout = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def create_checkout(self, intent):
        return self.checkout


class ModificationCheckoutViewServiceStub(CheckoutViewServiceStub):
    def create_modification_checkout(self, modification):
        return self.checkout


class ResultViewServiceStub:
    outcome = None
    calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def verify(self, attempt):
        type(self).calls += 1
        return self.outcome


@override_settings(**HYPERPAY_SETTINGS)
def test_widget_page_orders_mada_and_never_exposes_access_token(monkeypatch) -> None:
    client = Client()
    intent = payable_intent()
    own_intent(client, intent)
    attempt = PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="hyperpay",
        provider_checkout_id="checkout_12345678",
        merchant_transaction_id="LSA-view-test-123456",
        widget_integrity="sha384-YWJj",
        amount=intent.total_price,
        currency="SAR",
        status=PaymentAttempt.Status.PENDING,
        idempotency_key="view-test-idempotency-key-12345678",
    )
    CheckoutViewServiceStub.checkout = CheckoutSession(
        attempt,
        "checkout_12345678",
        "sha384-YWJj",
    )
    monkeypatch.setattr(HyperPayBookingCheckoutView, "service_class", CheckoutViewServiceStub)
    response = client.post(reverse("payments:hyperpay_booking", args=[intent.public_reference]))
    content = response.content.decode()
    assert response.status_code == 200
    assert content.index("var wpwlOptions") < content.index("paymentWidgets.js")
    assert content.index('data-brands="MADA"') < content.index('data-brands="VISA MASTER"')
    assert 'paymentTarget: "_top"' in content
    assert 'integrity="sha384-YWJj"' in content
    assert "test-access-token-secret" not in content
    csp = response.headers["Content-Security-Policy"]
    assert "https://eu-test.oppwa.com" in csp
    assert "form-action 'self' https://eu-test.oppwa.com" in csp
    assert "style-src 'self' 'unsafe-inline' https://eu-test.oppwa.com" in csp
    # The hosted widget ships English labels; the locale makes it follow the page.
    assert 'locale: "ar"' in content
    # One method is shown at a time, and the payer can confirm what they are buying.
    assert content.count('data-checkout-method="') == 2
    assert content.count('data-checkout-panel="') == 2
    assert intent.property.display_name in content or "checkout__summary" in content
    assert "checkout__total" in content
    assert reverse("reservations:intent_detail", args=[intent.public_reference]) in content
    assert "font-src 'self' data: https://eu-test.oppwa.com" in csp
    assert "unsafe-eval" not in csp


@override_settings(**HYPERPAY_SETTINGS)
def test_modification_page_opens_real_hyperpay_difference_checkout(monkeypatch) -> None:
    client = Client()
    reservation = confirmed_reservation()
    own_intent(client, reservation.booking_intent)
    modification = create_extension(reservation).request
    assert modification is not None
    attempt = PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        modification_request=modification,
        provider="hyperpay",
        provider_checkout_id="modification_checkout_12345678",
        merchant_transaction_id="LSA-modification-view-123456",
        widget_integrity="sha384-YWJj",
        amount=modification.price_difference,
        currency="SAR",
        status=PaymentAttempt.Status.PENDING,
        idempotency_key="modification-view-idempotency-key-12345678",
    )
    ModificationCheckoutViewServiceStub.checkout = CheckoutSession(
        attempt,
        attempt.provider_checkout_id,
        attempt.widget_integrity,
    )
    monkeypatch.setattr(
        HyperPayModificationCheckoutView,
        "service_class",
        ModificationCheckoutViewServiceStub,
    )
    response = client.post(
        reverse("payments:hyperpay_modification", args=[modification.public_reference])
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert "modification_checkout_12345678" in content
    assert "paymentWidgets.js" in content
    assert "test-access-token-secret" not in content
    # The shared checkout template must fall back to the change request, not the
    # booking intent, or the back link reverses against an empty reference.
    assert (
        reverse(
            "reservations:modification_detail",
            args=[modification.public_reference],
        )
        in content
    )


@override_settings(**HYPERPAY_SETTINGS)
def test_fake_browser_success_is_only_a_verification_trigger(monkeypatch) -> None:
    client = Client()
    intent = payable_intent()
    own_intent(client, intent)
    attempt = PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="hyperpay",
        provider_checkout_id="checkout_12345678",
        merchant_transaction_id="LSA-result-test-123456",
        amount=intent.total_price,
        currency="SAR",
        status=PaymentAttempt.Status.PENDING,
        idempotency_key="result-test-idempotency-key-123456",
    )
    ResultViewServiceStub.calls = 0
    ResultViewServiceStub.outcome = VerificationOutcome(attempt, HyperPayStatus.FAILED)
    monkeypatch.setattr(HyperPayResultView, "service_class", ResultViewServiceStub)
    response = client.get(
        reverse("payments:hyperpay_result", args=[attempt.pk]),
        {"success": "true"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert response.status_code == 200
    assert ResultViewServiceStub.calls == 1
    assert Reservation.objects.count() == 0
    assert "No booking was confirmed" in response.content.decode()


@override_settings(**HYPERPAY_SETTINGS)
def test_tampered_resource_path_and_idor_are_rejected(monkeypatch) -> None:
    owner = Client()
    intent = payable_intent()
    own_intent(owner, intent)
    attempt = PaymentAttempt.objects.create(
        booking_intent=intent,
        provider="hyperpay",
        provider_checkout_id="checkout_12345678",
        merchant_transaction_id="LSA-tamper-test-123456",
        amount=intent.total_price,
        currency="SAR",
        status=PaymentAttempt.Status.PENDING,
        idempotency_key="tamper-test-idempotency-key-123456",
    )
    ResultViewServiceStub.calls = 0
    monkeypatch.setattr(HyperPayResultView, "service_class", ResultViewServiceStub)
    url = reverse("payments:hyperpay_result", args=[attempt.pk])
    tampered = owner.get(url, {"resourcePath": "/v1/checkouts/other/payment"})
    stranger = Client().get(url)
    assert tampered.status_code == 404
    assert stranger.status_code == 404
    assert ResultViewServiceStub.calls == 0
