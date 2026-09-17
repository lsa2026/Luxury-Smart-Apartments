"""Automated HyperPay refund safeguards for Phase 9.4."""

from decimal import Decimal

import pytest
from django.test import override_settings

from apps.payments.hyperpay.refunds import HyperPayRefundService
from apps.payments.models import PaymentAttempt
from apps.reservations.models import RefundObligation
from apps.reservations.services.refunds import RefundComputation, record_obligation
from tests.test_booking_modifications_phase6 import confirmed_reservation

pytestmark = pytest.mark.django_db


REFUND_SETTINGS = {
    "HYPERPAY_ENABLED": True,
    "HYPERPAY_ENVIRONMENT": "test",
    "HYPERPAY_BASE_URL": "https://eu-test.oppwa.com/",
    "HYPERPAY_ENTITY_ID": "test-entity-id",
    "HYPERPAY_ACCESS_TOKEN": "test-access-token-secret",
    "HYPERPAY_CURRENCY": "SAR",
    "HYPERPAY_PAYMENT_TYPE": "DB",
    "HYPERPAY_REFUNDS_ENABLED": True,
    "HYPERPAY_REFUNDS_PRODUCTION_ENABLED": False,
}


class RefundStub:
    def __init__(self, document):
        self.document = document
        self.calls = []

    def refund_payment(self, payment_id, payload):
        self.calls.append((payment_id, payload))
        return self.document


def prepared_refund(amount=Decimal("400.0000")):
    reservation = confirmed_reservation()
    payment = PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        provider="hyperpay",
        provider_reference="checkout-refund-test",
        provider_payment_id="payment-refund-test",
        merchant_transaction_id="merchant-refund-test",
        amount=Decimal("1000.0000"),
        currency="SAR",
        status=PaymentAttempt.Status.SUCCEEDED,
        idempotency_key="refund-payment-attempt-test-000000000000000000000001",
    )
    refund = record_obligation(
        reservation,
        reason=RefundObligation.Reason.CANCELLATION,
        computation=RefundComputation(amount, "SAR", {"source": "test"}),
    )
    assert refund is not None
    return refund, payment


@override_settings(**REFUND_SETTINGS)
def test_partial_refund_calls_backoffice_api_then_marks_both_records():
    refund, payment = prepared_refund()
    stub = RefundStub(
        {
            "id": "refund-provider-123",
            "result": {"code": "000.100.110", "description": "request accepted"},
        }
    )

    outcome = HyperPayRefundService(client=stub).submit(refund, operator=object())

    assert outcome.audit_action == "refund.hyperpay_completed"
    assert stub.calls == [
        (
            "payment-refund-test",
            {
                "entityId": "test-entity-id",
                "amount": "400.00",
                "currency": "SAR",
                "paymentType": "RF",
            },
        )
    ]
    refund.refresh_from_db()
    payment.refresh_from_db()
    assert refund.status == RefundObligation.Status.TRANSFERRED
    assert refund.transfer_reference == "refund-provider-123"
    assert refund.calculation["hyperpay_refund"]["state"] == "confirmed"
    assert payment.status == PaymentAttempt.Status.PARTIALLY_REFUNDED


@override_settings(**REFUND_SETTINGS)
def test_full_refund_marks_original_payment_refunded():
    refund, payment = prepared_refund(Decimal("1000.0000"))
    stub = RefundStub({"id": "refund-full-123", "result": {"code": "000.100.110"}})

    HyperPayRefundService(client=stub).submit(refund, operator=object())

    payment.refresh_from_db()
    assert payment.status == PaymentAttempt.Status.REFUNDED


@override_settings(**REFUND_SETTINGS)
def test_rejected_response_leaves_refund_due_for_owner_review():
    refund, payment = prepared_refund()
    stub = RefundStub(
        {"id": "", "result": {"code": "800.100.100", "description": "not accepted"}}
    )

    outcome = HyperPayRefundService(client=stub).submit(refund, operator=object())

    assert outcome.audit_action == "refund.hyperpay_failed"
    refund.refresh_from_db()
    payment.refresh_from_db()
    assert refund.status == RefundObligation.Status.DUE
    assert refund.calculation["hyperpay_refund"]["state"] == "rejected"
    assert payment.status == PaymentAttempt.Status.SUCCEEDED


def test_production_refunds_stay_hidden_until_a_separate_explicit_opt_in():
    with override_settings(
        HYPERPAY_ENABLED=True,
        HYPERPAY_REFUNDS_ENABLED=True,
        HYPERPAY_ENVIRONMENT="production",
        HYPERPAY_REFUNDS_PRODUCTION_ENABLED=False,
    ):
        assert HyperPayRefundService.is_available() is False
