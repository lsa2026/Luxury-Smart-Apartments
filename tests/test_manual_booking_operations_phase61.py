"""Phase 6.1: owner-created drafts stay local, priced, and auditable."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import AuditLog
from apps.reservations.manual_bookings import (
    create_manual_booking_draft,
    finalize_manual_booking_draft,
)
from apps.reservations.models import (
    BookingQuote,
    HostawayReservationOperation,
    ManualBookingDraft,
    Reservation,
)
from tests.test_booking_models_services import make_availability, make_property

pytestmark = pytest.mark.django_db

OWNER_EMAIL = "saeed@luxurysmartapartments.com"


class FakeAvailabilityService:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def check(self, request, *, bypass_cache=False):
        self.requests.append((request, bypass_cache))
        return self.result


def owner():
    return get_user_model().objects.create_superuser(
        username="operations-owner",
        email=OWNER_EMAIL,
        password="not-used-by-the-workflow",
    )


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_manual_draft_uses_a_live_quote_but_creates_no_reservation_or_provider_operation():
    property_obj = make_property()
    actor = owner()
    available = make_availability(property_obj)
    service = FakeAvailabilityService(available)

    creation = create_manual_booking_draft(
        property_obj=property_obj,
        check_in=available.quote.check_in,
        check_out=available.quote.check_out,
        guests=available.quote.guests,
        actor=actor,
        availability_service=service,
    )

    assert creation.code == "created"
    assert creation.draft is not None
    draft = creation.draft
    assert draft.status == ManualBookingDraft.Status.QUOTED
    assert draft.system_total_price == available.quote.total_price
    assert draft.final_total_price == available.quote.total_price
    assert draft.price_source == ManualBookingDraft.PriceSource.SYSTEM
    assert service.requests[0][1] is True
    assert BookingQuote.objects.count() == 1
    assert Reservation.objects.count() == 0
    assert HostawayReservationOperation.objects.count() == 0


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_manual_price_override_requires_reason_and_preserves_system_price():
    property_obj = make_property()
    actor = owner()
    available = make_availability(property_obj, total=Decimal("500.25"))
    creation = create_manual_booking_draft(
        property_obj=property_obj,
        check_in=available.quote.check_in,
        check_out=available.quote.check_out,
        guests=available.quote.guests,
        actor=actor,
        availability_service=FakeAvailabilityService(available),
    )
    assert creation.draft is not None

    rejected = finalize_manual_booking_draft(
        draft_id=creation.draft.pk,
        guest_data={
            "guest_first_name": "Test",
            "guest_last_name": "Guest",
            "guest_email": "guest@example.invalid",
            "guest_phone": "+966500000000",
            "price_override_reason": "",
        },
        final_total_price=Decimal("450.00"),
    )
    assert rejected.code == "override_reason_required"

    finalized = finalize_manual_booking_draft(
        draft_id=creation.draft.pk,
        guest_data={
            "guest_first_name": "Test",
            "guest_last_name": "Guest",
            "guest_email": "guest@example.invalid",
            "guest_phone": "+966500000000",
            "price_override_reason": "تعويض الضيف عن مشكلة موثقة في الزيارة السابقة.",
            "special_requests": "Synthetic test data",
        },
        final_total_price=Decimal("450.00"),
    )
    assert finalized.code == "ready_for_payment"
    assert finalized.draft is not None
    finalized.draft.refresh_from_db()
    assert finalized.draft.status == ManualBookingDraft.Status.READY_FOR_PAYMENT
    assert finalized.draft.system_total_price == Decimal("500.2500")
    assert finalized.draft.final_total_price == Decimal("450.0000")
    assert finalized.draft.price_source == ManualBookingDraft.PriceSource.MANUAL_OVERRIDE
    assert finalized.draft.payment_amount_sar == Decimal("450.00")
    assert Reservation.objects.count() == 0


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_non_owner_admin_cannot_open_manual_booking_screens():
    other_admin = get_user_model().objects.create_superuser(
        username="other-admin",
        email="other-admin@example.invalid",
        password="pw12345!",
    )
    client = Client()
    client.force_login(other_admin)

    assert client.get(reverse("notifications:manual_booking_list")).status_code == 403
    assert client.get(reverse("notifications:manual_booking_create")).status_code == 403


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_owner_create_screen_records_a_privacy_safe_audit_event(monkeypatch):
    property_obj = make_property()
    actor = owner()
    available = make_availability(property_obj)
    creation = create_manual_booking_draft(
        property_obj=property_obj,
        check_in=available.quote.check_in,
        check_out=available.quote.check_out,
        guests=available.quote.guests,
        actor=actor,
        availability_service=FakeAvailabilityService(available),
    )
    assert creation.draft is not None

    def fake_create(*, property_obj, check_in, check_out, guests, actor):
        return creation

    monkeypatch.setattr(
        "apps.reservations.operations_views.create_manual_booking_draft",
        fake_create,
    )
    client = Client()
    client.force_login(actor)
    response = client.post(
        reverse("notifications:manual_booking_create"),
        {
            "property": property_obj.pk,
            "check_in": available.quote.check_in.isoformat(),
            "check_out": available.quote.check_out.isoformat(),
            "guests": available.quote.guests,
        },
    )

    assert response.status_code == 302
    audit = AuditLog.objects.get(action="manual_booking.availability_checked")
    assert audit.actor_user == actor
    assert audit.object_reference == creation.draft.public_reference
    assert "guest" not in audit.summary.casefold()


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_owner_can_cancel_a_manual_draft_and_see_its_audit_history():
    property_obj = make_property()
    actor = owner()
    creation = create_manual_booking_draft(
        property_obj=property_obj,
        check_in=timezone.localdate() + timedelta(days=10),
        check_out=timezone.localdate() + timedelta(days=12),
        guests=1,
        actor=actor,
        availability_service=FakeAvailabilityService(make_availability(property_obj)),
    )
    assert creation.draft is not None
    draft = creation.draft
    client = Client()
    client.force_login(actor)

    response = client.post(
        reverse("notifications:manual_booking_detail", args=[draft.pk]),
        {"action": "cancel", "confirm_cancellation": "on"},
    )

    assert response.status_code == 302
    draft.refresh_from_db()
    assert draft.status == ManualBookingDraft.Status.CANCELLED
    assert AuditLog.objects.filter(
        action="manual_booking.cancelled",
        object_reference=draft.public_reference,
    ).exists()
    page = client.get(reverse("notifications:manual_booking_detail", args=[draft.pk]))
    assert "تم إلغاء المسودة الداخلية قبل الدفع." in page.content.decode()


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_owner_can_find_manual_drafts_by_guest_name():
    property_obj = make_property()
    actor = owner()
    creation = create_manual_booking_draft(
        property_obj=property_obj,
        check_in=timezone.localdate() + timedelta(days=10),
        check_out=timezone.localdate() + timedelta(days=12),
        guests=1,
        actor=actor,
        availability_service=FakeAvailabilityService(make_availability(property_obj)),
    )
    assert creation.draft is not None
    draft = creation.draft
    draft.guest_first_name = "Aseel"
    draft.guest_last_name = "Hafez"
    draft.save(update_fields=["guest_first_name", "guest_last_name", "updated_at"])

    client = Client()
    client.force_login(actor)
    page = client.get(reverse("notifications:manual_booking_list"), {"q": "Aseel"})

    assert page.status_code == 200
    assert "Aseel Hafez" in page.content.decode()


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_owner_can_permanently_delete_an_uncharged_manual_draft():
    property_obj = make_property()
    actor = owner()
    creation = create_manual_booking_draft(
        property_obj=property_obj,
        check_in=timezone.localdate() + timedelta(days=10),
        check_out=timezone.localdate() + timedelta(days=12),
        guests=1,
        actor=actor,
        availability_service=FakeAvailabilityService(make_availability(property_obj)),
    )
    assert creation.draft is not None
    draft = creation.draft
    quote_id = draft.quote_id
    public_reference = draft.public_reference

    client = Client()
    client.force_login(actor)
    response = client.post(
        reverse("notifications:manual_booking_detail", args=[draft.pk]),
        {"action": "delete", "confirm_deletion": "on"},
    )

    assert response.status_code == 302
    assert not ManualBookingDraft.objects.filter(pk=draft.pk).exists()
    assert not BookingQuote.objects.filter(pk=quote_id).exists()
    assert AuditLog.objects.filter(
        action="manual_booking.deleted",
        object_reference=public_reference,
    ).exists()


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_manual_booking_create_screen_exposes_live_calendar_for_each_property():
    make_property()
    actor = owner()
    client = Client()
    client.force_login(actor)

    page = client.get(reverse("notifications:manual_booking_create"))

    content = page.content.decode()
    assert page.status_code == 200
    assert 'id="manual-booking-calendar-urls"' in content
    assert "data-luxury-calendar" in content
    assert "data-calendar-property-select" in content
