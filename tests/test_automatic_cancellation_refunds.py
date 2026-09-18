"""The guest cancellation path cancels first, then refunds the original card."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.integrations.hostaway.reservation_validators import HostawayReservationSnapshot
from apps.payments.hyperpay.refunds import HyperPayRefundService
from apps.payments.models import PaymentAttempt
from apps.reservations.models import CancellationPolicyTier, RefundObligation
from apps.reservations.services.automatic_modifications import execute_automatic_modification
from apps.reservations.services.hostaway_modifications import HostawayModificationService
from apps.reservations.services.modifications import ModificationService
from tests.test_booking_modifications_phase6 import (
    ModificationAvailabilityStub,
    WriteClientStub,
    confirmed_reservation,
    price_quote,
    updated_snapshot,
)
from tests.test_hyperpay_refunds_phase94 import REFUND_SETTINGS, RefundStub

pytestmark = pytest.mark.django_db


@override_settings(
    **{
        **REFUND_SETTINGS,
        "HYPERPAY_ENVIRONMENT": "production",
        "HYPERPAY_BASE_URL": "https://eu-prod.oppwa.com/",
        "HYPERPAY_REFUNDS_PRODUCTION_ENABLED": True,
        "BOOKING_AUTOMATIC_MODIFICATION_APPROVAL": False,
        "BOOKING_AUTOMATIC_CANCELLATION_ENABLED": True,
        "BOOKING_AUTOMATIC_REFUND_ENABLED": True,
        "HOSTAWAY_LIVE_CANCELLATION_ENABLED": True,
    }
)
def test_automatic_guest_cancellation_refunds_only_after_hostaway_confirms():
    reservation = confirmed_reservation()
    reservation.property.cancellation_policy = "flexible"
    reservation.property.save(update_fields=["cancellation_policy"])
    CancellationPolicyTier.objects.create(
        policy_code="flexible",
        min_hours_before_check_in=0,
        refund_percentage=Decimal("100"),
        refunds_cleaning_fee=True,
        is_active=True,
    )
    PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        provider="hyperpay",
        provider_payment_id="original-payment-automatic-cancellation",
        amount=reservation.total_price,
        currency="SAR",
        status=PaymentAttempt.Status.SUCCEEDED,
        verified_at=timezone.now(),
        idempotency_key="automatic-cancellation-original-payment-000000000001",
    )
    modification = ModificationService().create_cancellation_request(
        reservation,
        session_hash=reservation.booking_intent.session_key_hash,
    ).request
    assert modification is not None
    assert modification.status == modification.Status.READY_FOR_HOSTAWAY
    snapshot = HostawayReservationSnapshot(
        reservation_id=reservation.hostaway_reservation_id,
        listing_map_id=reservation.hostaway_listing_map_id,
        channel_id=2000,
        status="cancelled",
        check_in=reservation.check_in,
        check_out=reservation.check_out,
        guests=reservation.guests,
        currency="SAR",
        total_price=reservation.total_price,
        payment_status="paid",
        source="LuxurySmartApartments",
        updated_at=timezone.now(),
    )
    hostaway = HostawayModificationService(client=WriteClientStub(snapshot=snapshot))
    hyperpay = HyperPayRefundService(
        client=RefundStub({"id": "automatic-refund-123", "result": {"code": "000.100.110"}})
    )

    outcome = execute_automatic_modification(
        modification,
        service=hostaway,
        refund_service=hyperpay,
    )

    reservation.refresh_from_db()
    assert outcome.code == "completed"
    assert outcome.refund_code == "refund.hyperpay_completed"
    assert reservation.normalized_status == reservation.Status.CANCELLED
    assert outcome.refund is not None
    outcome.refund.refresh_from_db()
    assert outcome.refund.status == RefundObligation.Status.TRANSFERRED


@override_settings(
    **{
        **REFUND_SETTINGS,
        "HYPERPAY_ENVIRONMENT": "production",
        "HYPERPAY_BASE_URL": "https://eu-prod.oppwa.com/",
        "HYPERPAY_REFUNDS_PRODUCTION_ENABLED": True,
        "BOOKING_AUTOMATIC_MODIFICATION_APPROVAL": True,
        "BOOKING_AUTOMATIC_REFUND_ENABLED": True,
        "HOSTAWAY_LIVE_MODIFICATION_ENABLED": True,
        "HOSTAWAY_LIVE_EXTENSION_ENABLED": True,
    }
)
def test_automatic_price_decrease_refunds_only_after_hostaway_confirms():
    reservation = confirmed_reservation()
    reservation.total_price = Decimal("1000.0000")
    reservation.save(update_fields=["total_price", "updated_at"])
    PaymentAttempt.objects.create(
        booking_intent=reservation.booking_intent,
        provider="hyperpay",
        provider_payment_id="original-payment-automatic-decrease",
        amount=reservation.total_price,
        currency="SAR",
        status=PaymentAttempt.Status.SUCCEEDED,
        verified_at=timezone.now(),
        idempotency_key="automatic-decrease-original-payment-000000000001",
    )
    new_check_out = reservation.check_out + timedelta(days=1)
    availability = ModificationAvailabilityStub(
        reservation,
        quote=price_quote(
            reservation,
            check_out=new_check_out,
            total=Decimal("800.0000"),
        ),
    )
    modification = ModificationService(availability_service=availability).create_change_quote(
        reservation,
        new_check_in=reservation.check_in,
        new_check_out=new_check_out,
        new_guests=reservation.guests,
        session_hash=reservation.booking_intent.session_key_hash,
    ).request
    assert modification is not None
    assert modification.price_difference == Decimal("-200.0000")
    assert modification.status == modification.Status.READY_FOR_HOSTAWAY
    hostaway = HostawayModificationService(
        client=WriteClientStub(snapshot=updated_snapshot(modification))
    )
    hyperpay = HyperPayRefundService(
        client=RefundStub(
            {"id": "automatic-decrease-refund-123", "result": {"code": "000.100.110"}}
        )
    )

    outcome = execute_automatic_modification(
        modification,
        service=hostaway,
        refund_service=hyperpay,
    )

    assert outcome.code == "completed"
    assert outcome.refund_code == "refund.hyperpay_completed"
    assert outcome.refund is not None
    outcome.refund.refresh_from_db()
    assert outcome.refund.status == RefundObligation.Status.TRANSFERRED
