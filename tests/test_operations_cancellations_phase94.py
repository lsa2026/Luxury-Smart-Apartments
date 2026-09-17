"""Phase 9.4: owner-only cancellation and refund review screens."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import AuditLog
from apps.reservations.models import BookingModificationRequest, RefundObligation
from apps.reservations.services.refunds import RefundComputation, record_obligation
from tests.test_booking_modifications_phase6 import confirmed_reservation

pytestmark = pytest.mark.django_db

OWNER_EMAIL = "saeed@luxurysmartapartments.com"


def owner():
    return get_user_model().objects.create_superuser(
        username="phase94-owner",
        email=OWNER_EMAIL,
        password="not-used-by-the-workflow",
    )


def cancellation_request():
    reservation = confirmed_reservation()
    return BookingModificationRequest.objects.create(
        reservation=reservation,
        request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION,
        status=BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL,
        old_check_in=reservation.check_in,
        old_check_out=reservation.check_out,
        old_guests=reservation.guests,
        old_total=reservation.total_price,
        price_difference=Decimal("0"),
        currency=reservation.currency,
        reason="اختبار طلب إلغاء",
        quote_snapshot={},
        idempotency_key="phase94-cancellation-" + "x" * 40,
        session_key_hash="phase94-session-hash",
        expires_at=timezone.now() + timedelta(days=1),
    )


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_only_owner_can_open_cancellation_queue_and_the_guest_name_is_primary():
    request = cancellation_request()
    owner_client = Client()
    owner_client.force_login(owner())

    page = owner_client.get(reverse("notifications:cancellation_list"))

    content = page.content.decode()
    assert page.status_code == 200
    assert request.reservation.booking_intent.guest_first_name in content
    assert reverse("notifications:cancellation_detail", args=[request.pk]) in content


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_owner_approval_prepares_but_does_not_send_a_cancellation():
    request = cancellation_request()
    client = Client()
    client.force_login(owner())

    response = client.post(
        reverse("notifications:cancellation_detail", args=[request.pk]),
        {"action": "approve", "decision_note": "تمت مراجعة طلب الضيف واعتماده داخليًا."},
    )

    assert response.status_code == 302
    request.refresh_from_db()
    assert request.status == BookingModificationRequest.Status.READY_FOR_HOSTAWAY
    assert AuditLog.objects.filter(
        action="cancellation.approved_locally", object_reference=request.public_reference
    ).exists()


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_owner_can_reject_a_cancellation_with_an_audited_reason():
    request = cancellation_request()
    client = Client()
    client.force_login(owner())

    response = client.post(
        reverse("notifications:cancellation_detail", args=[request.pk]),
        {"action": "reject", "decision_note": "الحجز أصبح ضمن فترة غير قابلة للاسترداد."},
    )

    assert response.status_code == 302
    request.refresh_from_db()
    assert request.status == BookingModificationRequest.Status.REJECTED
    assert AuditLog.objects.filter(
        action="cancellation.rejected_locally", object_reference=request.public_reference
    ).exists()


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_owner_can_approve_partial_refund_then_record_an_external_transfer():
    reservation = confirmed_reservation()
    refund = record_obligation(
        reservation,
        reason=RefundObligation.Reason.CANCELLATION,
        computation=RefundComputation(Decimal("1000.0000"), "SAR", {"source": "test"}),
    )
    assert refund is not None
    client = Client()
    client.force_login(owner())

    approval = client.post(
        reverse("notifications:refund_detail", args=[refund.pk]),
        {
            "action": "approve_amount",
            "approved_amount": "425.00",
            "decision_note": "اتفاق استثنائي موثق مع الضيف على استرداد جزئي.",
        },
    )

    assert approval.status_code == 302
    refund.refresh_from_db()
    assert refund.amount == Decimal("425.0000")
    assert refund.calculation["operator_decision"]["original_amount"] == "1000.0000"
    transfer = client.post(
        reverse("notifications:refund_detail", args=[refund.pk]),
        {
            "action": "mark_transferred",
            "transfer_reference": "BANK-REF-2026-001",
            "settlement_note": "تم التحويل إلى حساب الضيف.",
            "confirm_settlement": "on",
        },
    )

    assert transfer.status_code == 302
    refund.refresh_from_db()
    assert refund.status == RefundObligation.Status.TRANSFERRED
    assert refund.transfer_reference == "BANK-REF-2026-001"
    assert AuditLog.objects.filter(
        action="refund.marked_transferred", object_reference=refund.public_reference
    ).exists()
