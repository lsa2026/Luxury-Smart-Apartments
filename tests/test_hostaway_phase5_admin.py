import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from apps.integrations.admin import HostawayWebhookEventAdmin
from apps.integrations.models import HostawayWebhookEvent
from apps.reservations.admin import (
    HostawayReservationOperationAdmin,
    ReservationAdmin,
)
from apps.reservations.models import HostawayReservationOperation, Reservation

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
