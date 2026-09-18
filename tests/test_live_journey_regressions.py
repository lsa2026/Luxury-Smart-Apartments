"""Regressions found in the paid 1090 -> 1080 -> cancellation journey."""

from decimal import Decimal

import pytest
from django.template.loader import render_to_string
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from apps.payments.hyperpay.refunds import HyperPayRefundService
from apps.payments.models import PaymentAttempt
from apps.reservations.models import RefundObligation
from apps.reservations.services.refunds import cancellation_refund
from tests.test_hyperpay_refunds_phase94 import REFUND_SETTINGS, RefundStub
from tests.test_operations_cancellations_phase94 import cancellation_request, owner

pytestmark = pytest.mark.django_db


def partially_refunded_booking():
    request = cancellation_request()
    reservation = request.reservation
    reservation.total_price = Decimal("1080")
    reservation.save(update_fields=["total_price"])
    payment = PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        provider="hyperpay",
        provider_payment_id="live-regression-original",
        amount=Decimal("1090"),
        currency="SAR",
        status=PaymentAttempt.Status.PARTIALLY_REFUNDED,
        verified_at=timezone.now(),
        idempotency_key="live-regression-payment",
    )
    prior = RefundObligation.objects.create(
        reservation=reservation,
        amount=Decimal("10"),
        currency="SAR",
        reason=RefundObligation.Reason.MODIFICATION_DECREASE,
        status=RefundObligation.Status.TRANSFERRED,
        transferred_at=timezone.now(),
        transfer_reference="prior-refund",
        calculation={"hyperpay_refund": {"original_payment_id": payment.provider_payment_id}},
    )
    return request, payment, prior


@override_settings(BOOKING_LAUNCH_FLEXIBLE_CANCELLATION_ENABLED=True)
def test_partial_refund_remains_paid_and_cancellation_offers_only_net_balance():
    request, payment, prior = partially_refunded_booking()
    assert cancellation_refund(request.reservation).amount == Decimal("1080")
    client = Client()
    client.force_login(owner())
    response = client.get(reverse("notifications:cancellation_detail", args=[request.pk]))
    assert response.context["has_successful_payment"] is True
    assert response.context["estimated_refund"].amount == Decimal("1080")
    assert "لم تُسجّل دفعة ناجحة" not in response.content.decode()
    response = client.get(reverse("notifications:booking_list"))
    assert response.status_code == 200


@override_settings(**REFUND_SETTINGS, BOOKING_LAUNCH_FLEXIBLE_CANCELLATION_ENABLED=True)
def test_final_refund_returns_only_1080_and_never_repeats_prior_10():
    request, payment, prior = partially_refunded_booking()
    refund = RefundObligation.objects.create(
        reservation=request.reservation,
        amount=cancellation_refund(request.reservation).amount,
        currency="SAR",
        reason=RefundObligation.Reason.CANCELLATION,
    )
    stub = RefundStub({"id": "final-refund", "result": {"code": "000.100.110"}})
    service = HyperPayRefundService(client=stub)
    assert service.submit(refund, operator=None).audit_action == "refund.hyperpay_completed"
    assert stub.calls[0][1]["amount"] == "1080.00"
    payment.refresh_from_db()
    assert payment.status == PaymentAttempt.Status.REFUNDED
    assert cancellation_refund(request.reservation).amount == 0
    from apps.payments.hyperpay.exceptions import HyperPayRefundError

    with pytest.raises(HyperPayRefundError):
        service.submit(refund, operator=None)
    assert len(stub.calls) == 1


@override_settings(BOOKING_LAUNCH_FLEXIBLE_CANCELLATION_ENABLED=True)
def test_inflight_refund_is_reserved_and_cannot_be_refunded_twice():
    request, payment, prior = partially_refunded_booking()
    prior.status = RefundObligation.Status.PROCESSING
    prior.save(update_fields=["status"])
    assert cancellation_refund(request.reservation).amount == Decimal("1080")


@pytest.mark.parametrize("language", ["ar", "fr", "en"])
def test_purchase_machine_amount_is_not_localized(language):
    with translation.override(language):
        html = render_to_string(
            "payments/hyperpay_result.html",
            {
                "purchase_event": {"value": 1090.0, "currency": "SAR", "transaction_id": "test"},
                "attempt": {"currency": "SAR", "amount": Decimal("1090")},
            },
        )
    assert 'data-analytics-value="1090.0"' in html
    assert 'data-analytics-value="1090,0"' not in html
