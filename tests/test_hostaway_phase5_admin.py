from types import SimpleNamespace

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from apps.integrations.admin import HostawayWebhookEventAdmin
from apps.integrations.models import HostawayWebhookEvent
from apps.payments.admin import PaymentAttemptAdmin
from apps.payments.models import PaymentAttempt
from apps.reservations.admin import (
    BookingIntentAdmin,
    BookingModificationRequestAdmin,
    HostawayModificationOperationAdmin,
    HostawayReservationOperationAdmin,
    ManualBookingDraftAdmin,
    RefundObligationAdmin,
    ReservationAdmin,
)
from apps.reservations.models import (
    BookingIntent,
    BookingModificationRequest,
    HostawayModificationOperation,
    HostawayReservationOperation,
    ManualBookingDraft,
    RefundObligation,
    Reservation,
)

pytestmark = pytest.mark.django_db


def test_phase5_admin_models_are_read_only_and_block_manual_add() -> None:
    user = get_user_model().objects.create_superuser(
        username="phase5-admin",
        email="phase5-admin@example.invalid",
        password="synthetic-password",
    )
    request = RequestFactory().get("/admin/")
    request.user = user
    reservation_admin = ReservationAdmin(Reservation, AdminSite())
    operation_admin = HostawayReservationOperationAdmin(
        HostawayReservationOperation,
        AdminSite(),
    )
    webhook_admin = HostawayWebhookEventAdmin(HostawayWebhookEvent, AdminSite())

    assert reservation_admin.has_add_permission(request) is False
    assert reservation_admin.has_delete_permission(request) is False
    assert {field.name for field in Reservation._meta.fields}.issubset(
        set(reservation_admin.readonly_fields)
    )
    assert reservation_admin.has_change_permission(request) is False

    assert operation_admin.has_add_permission(request) is False
    assert operation_admin.has_delete_permission(request) is False
    assert "request_fingerprint" not in operation_admin.readonly_fields
    assert "fingerprint_preview" in operation_admin.readonly_fields

    assert webhook_admin.has_add_permission(request) is False
    assert webhook_admin.has_delete_permission(request) is False
    assert "sanitized_payload" not in webhook_admin.fields
    assert "payload_summary" in webhook_admin.readonly_fields


def test_hostaway_operation_list_prioritizes_guest_name_over_reference() -> None:
    operation_admin = HostawayReservationOperationAdmin(
        HostawayReservationOperation,
        AdminSite(),
    )
    operation = SimpleNamespace(
        reservation=SimpleNamespace(
            booking_intent_id="intent-id",
            booking_intent=SimpleNamespace(
                guest_first_name="Saeed",
                guest_last_name="Alghamdi",
            ),
        )
    )
    external_operation = SimpleNamespace(
        reservation=SimpleNamespace(booking_intent_id=None),
    )

    assert operation_admin.list_display[0] == "guest_name"
    assert operation_admin.guest_name(operation) == "Saeed Alghamdi"
    assert operation_admin.guest_name(external_operation) == "—"
    assert "reservation__booking_intent__guest_first_name" in operation_admin.search_fields


def test_every_booking_operations_list_prioritizes_the_guest_name() -> None:
    site = AdminSite()
    request = RequestFactory().get("/admin/")
    request.user = get_user_model().objects.create_superuser(
        username="owner",
        email="owner@example.invalid",
        password="synthetic-password",
    )
    intent = SimpleNamespace(
        guest_first_name="Saeed",
        guest_last_name="Alghamdi",
        public_reference="BK-TEST-123",
    )
    reservation = SimpleNamespace(booking_intent_id="intent-id", booking_intent=intent)
    draft = SimpleNamespace(guest_first_name="Saeed", guest_last_name="Alghamdi")
    modification = SimpleNamespace(reservation=reservation)

    assert ManualBookingDraftAdmin(ManualBookingDraft, site).list_display[0] == "guest_name"
    assert BookingIntentAdmin(BookingIntent, site).get_list_display(request)[0] == "guest_name_list"
    assert ReservationAdmin(Reservation, site).guest_name(reservation) == "Saeed Alghamdi"
    assert BookingModificationRequestAdmin(
        BookingModificationRequest, site
    ).guest_name(SimpleNamespace(reservation=reservation)) == "Saeed Alghamdi"
    assert HostawayModificationOperationAdmin(
        HostawayModificationOperation, site
    ).guest_name(SimpleNamespace(modification_request=modification)) == "Saeed Alghamdi"
    assert RefundObligationAdmin(RefundObligation, site).guest_name_display(
        SimpleNamespace(guest_name="Saeed Alghamdi")
    ) == "Saeed Alghamdi"
    assert ManualBookingDraftAdmin(ManualBookingDraft, site).guest_name(draft) == "Saeed Alghamdi"
    payment = SimpleNamespace(booking_intent=intent, booking_intent_id="intent-id")
    payment_admin = PaymentAttemptAdmin(PaymentAttempt, site)
    assert payment_admin.list_display[0] == "guest_name"
    assert "booking_intent__guest_first_name" in payment_admin.search_fields
    assert "Saeed Alghamdi" in str(payment_admin.guest_name(payment))
