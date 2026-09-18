"""Phase 6.1: owner-created drafts stay local, priced, and auditable."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import AuditLog
from apps.reservations.manual_bookings import (
    ManualBookingHostawayCreation,
    create_manual_booking_draft,
    create_manual_booking_in_hostaway,
    finalize_manual_booking_draft,
    recheck_manual_booking_draft,
)
from apps.reservations.models import (
    BookingIntent,
    BookingQuote,
    HostawayReservationOperation,
    ManualBookingDraft,
    Reservation,
)
from apps.reservations.operations_forms import ManualBookingFinalizeForm
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


class FakeManualHostawayBookingService:
    def create_manual_reservation_before_payment(self, reservation):
        reservation.hostaway_reservation_id = 88001
        reservation.hostaway_status = "new"
        reservation.payment_status = "unpaid"
        reservation.normalized_status = Reservation.Status.CONFIRMED
        reservation.confirmed_at = timezone.now()
        reservation.save()
        return type("Outcome", (), {"code": "confirmed", "reservation": reservation})()


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
def test_manual_price_override_is_allowed_without_a_reason_and_preserves_system_price():
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

    finalized = finalize_manual_booking_draft(
        draft_id=creation.draft.pk,
        guest_data={
            "guest_first_name": "Test",
            "guest_last_name": "Guest",
            "guest_email": "guest@example.invalid",
            "guest_phone": "+966500000000",
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
    assert finalized.draft.price_override_reason == ""
    assert finalized.draft.special_requests == ""
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

    captured: dict[str, object] = {}

    def fake_create(*, property_obj, check_in, check_out, guests, actor):
        captured["guests"] = guests
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
        },
    )

    assert response.status_code == 302
    audit = AuditLog.objects.get(action="manual_booking.availability_checked")
    assert audit.actor_user == actor
    assert audit.object_reference == creation.draft.public_reference
    assert "guest" not in audit.summary.casefold()
    assert captured["guests"] == 1


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_retired_draft_disposal_route_redirects_without_changing_booking_setup():
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

    response = client.post(reverse("notifications:manual_booking_disposal", args=[draft.pk]))

    assert response.status_code == 302
    assert response["Location"] == reverse("notifications:booking_list")
    draft.refresh_from_db()
    assert draft.status == ManualBookingDraft.Status.QUOTED


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_retired_draft_list_redirects_to_the_single_booking_workspace():
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

    assert page.status_code == 302
    assert page["Location"] == reverse("notifications:booking_list")


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_retired_draft_disposal_never_deletes_an_in_progress_booking_setup():
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
    response = client.post(reverse("notifications:manual_booking_disposal", args=[draft.pk]))

    assert response.status_code == 302
    assert response["Location"] == reverse("notifications:booking_list")
    assert ManualBookingDraft.objects.filter(pk=draft.pk).exists()


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
    assert "data-manual-property-filter" in content
    assert "عدد الضيوف" not in content
    assert "data-manual-property-results" in content


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_manual_draft_can_be_rechecked_without_creating_a_reservation():
    property_obj = make_property()
    actor = owner()
    available = make_availability(property_obj)
    initial = create_manual_booking_draft(
        property_obj=property_obj,
        check_in=available.quote.check_in,
        check_out=available.quote.check_out,
        guests=2,
        actor=actor,
        availability_service=FakeAvailabilityService(available),
    )
    assert initial.draft is not None
    rechecked = recheck_manual_booking_draft(
        draft_id=initial.draft.pk,
        actor=actor,
        availability_service=FakeAvailabilityService(available),
    )

    assert rechecked.code == "rechecked"
    assert rechecked.draft is not None
    assert rechecked.draft.status == ManualBookingDraft.Status.QUOTED
    assert Reservation.objects.count() == 0


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_new_booking_form_uses_full_width_email_and_two_decimal_owner_price():
    property_obj = make_property()
    actor = owner()
    available = make_availability(property_obj, total=Decimal("8883.0000"))
    creation = create_manual_booking_draft(
        property_obj=property_obj,
        check_in=available.quote.check_in,
        check_out=available.quote.check_out,
        guests=1,
        actor=actor,
        availability_service=FakeAvailabilityService(available),
    )
    assert creation.draft is not None

    form = ManualBookingFinalizeForm(draft=creation.draft)
    assert form.fields["final_total_price"].decimal_places == 2
    assert form["final_total_price"].value() == Decimal("8883.00")
    assert "lsa-manual-booking__email-input" in form["guest_email"].as_widget()

    client = Client()
    client.force_login(actor)
    page = client.get(reverse("notifications:manual_booking_detail", args=[creation.draft.pk]))
    content = page.content.decode()
    assert "إتمام حجز جديد" in content
    assert "سجل العملية" in content
    assert "سجل المسودة" not in content


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_ready_manual_draft_creates_an_unpaid_hostaway_booking_before_payment():
    property_obj = make_property()
    actor = owner()
    available = make_availability(property_obj, total=Decimal("500.25"))
    creation = create_manual_booking_draft(
        property_obj=property_obj,
        check_in=available.quote.check_in,
        check_out=available.quote.check_out,
        guests=1,
        actor=actor,
        availability_service=FakeAvailabilityService(available),
    )
    assert creation.draft is not None
    finalized = finalize_manual_booking_draft(
        draft_id=creation.draft.pk,
        guest_data={
            "guest_first_name": "Test",
            "guest_last_name": "Guest",
            "guest_email": "guest@example.invalid",
            "guest_phone": "+966500000000",
        },
        final_total_price=Decimal("450.00"),
    )
    assert finalized.draft is not None

    created = create_manual_booking_in_hostaway(
        draft_id=finalized.draft.pk,
        booking_service=FakeManualHostawayBookingService(),
    )

    assert created.code == "created"
    assert created.reservation is not None
    assert created.reservation.hostaway_reservation_id == 88001
    assert created.reservation.payment_status == "unpaid"
    assert created.reservation.normalized_status == Reservation.Status.CONFIRMED
    assert created.draft is not None
    assert created.draft.status == ManualBookingDraft.Status.BOOKED_AWAITING_PAYMENT
    intent = BookingIntent.objects.get(quote=creation.draft.quote)
    assert intent.total_price == Decimal("450.0000")
    assert intent.status == BookingIntent.Status.AWAITING_PAYMENT


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
    ACCOUNTING_WHATSAPP_NAME="Aseel Hafez",
    ACCOUNTING_WHATSAPP_NUMBER="+966597193102",
)
def test_ready_manual_draft_requires_hostaway_confirmation_before_the_accounting_request():
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
    finalized = finalize_manual_booking_draft(
        draft_id=creation.draft.pk,
        guest_data={
            "guest_first_name": "Test",
            "guest_last_name": "Guest",
            "guest_email": "guest@example.invalid",
            "guest_phone": "+966500000000",
        },
        final_total_price=available.quote.total_price,
    )
    assert finalized.draft is not None
    client = Client()
    client.force_login(actor)

    page = client.get(reverse("notifications:manual_booking_detail", args=[creation.draft.pk]))

    content = page.content.decode()
    assert "تأكيد الحجز في Hostaway وإرسال الطلب للمحاسبة" in content
    assert "لا تُرسل الرسالة تلقائيًا" not in content
    assert "سبب تعديل السعر" not in content
    assert "ملاحظات تشغيلية" not in content
    assert "إلغاء المسودة" not in content
    assert "حذف المسودة نهائيًا" not in content


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
    ACCOUNTING_WHATSAPP_NAME="Aseel Hafez",
    ACCOUNTING_WHATSAPP_NUMBER="+966597193102",
)
def test_owner_confirms_hostaway_booking_then_sends_the_accounting_request(monkeypatch):
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
    draft.status = ManualBookingDraft.Status.READY_FOR_PAYMENT
    draft.guest_first_name = "Test"
    draft.guest_last_name = "Guest"
    draft.guest_email = "guest@example.invalid"
    draft.guest_phone = "+966500000000"
    draft.save()
    reservation = Reservation.objects.create(
        booking_intent=BookingIntent.objects.create(
            quote=draft.quote,
            property=property_obj,
            check_in=draft.check_in,
            check_out=draft.check_out,
            nights=draft.nights,
            guests=draft.guests,
            currency=draft.currency,
            total_price=draft.final_total_price,
            payment_amount_sar=draft.payment_amount_sar,
            selected_display_currency=draft.selected_display_currency,
            exchange_rate_snapshot=draft.exchange_rate_snapshot,
            guest_first_name=draft.guest_first_name,
            guest_last_name=draft.guest_last_name,
            guest_email=draft.guest_email,
            guest_phone=draft.guest_phone,
            guest_country_code="SA",
            billing_street1="Not provided",
            billing_city="Not provided",
            billing_state="Not provided",
            billing_country="SA",
            billing_postcode="Not provided",
            language="ar",
            idempotency_key="phase61-accounting-request-key-000000000000000000000000",
            session_key_hash=draft.quote.session_key_hash,
            terms_accepted_at=timezone.now(), privacy_accepted_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=10),
        ),
        property=property_obj,
        hostaway_listing_id=property_obj.hostaway_listing_id,
        hostaway_listing_map_id=property_obj.hostaway_listing_map_id,
        source_type=Reservation.SourceType.DIRECT_WEBSITE,
        normalized_status=Reservation.Status.CONFIRMED,
        check_in=draft.check_in, check_out=draft.check_out, nights=draft.nights,
        guests=draft.guests, currency=draft.currency, total_price=draft.final_total_price,
        hostaway_reservation_id=88001, payment_status="unpaid", confirmed_at=timezone.now(),
    )
    monkeypatch.setattr(
        "apps.reservations.operations_views.create_manual_booking_in_hostaway",
        lambda **_kwargs: ManualBookingHostawayCreation("already_created", draft, reservation),
    )
    monkeypatch.setattr(
        "apps.reservations.operations_views.send_manual_payment_link_request",
        lambda **_kwargs: SimpleNamespace(code="sent"),
    )
    client = Client()
    client.force_login(actor)

    response = client.post(
        reverse("notifications:manual_booking_detail", args=[draft.pk]),
        {"action": "create_hostaway_and_request_payment"},
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("notifications:booking_list")
    assert AuditLog.objects.filter(action="manual_booking.accounting_whatsapp_sent").exists()


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_cancellation_workspace_does_not_list_booking_setups():
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
    page = client.get(reverse("notifications:cancellation_list"))

    content = page.content.decode()
    assert page.status_code == 200
    assert "مسودات الحجز اليدوي" not in content
    assert "Aseel Hafez" not in content
    assert reverse("notifications:manual_booking_disposal", args=[draft.pk]) not in content


@override_settings(
    OPERATIONS_OWNER_ENFORCEMENT_ENABLED=True,
    OPERATIONS_OWNER_EMAIL=OWNER_EMAIL,
)
def test_manual_property_picker_returns_live_priced_choices(monkeypatch):
    actor = owner()
    property_obj = make_property()
    client = Client()
    client.force_login(actor)
    monkeypatch.setattr(
        "apps.reservations.operations_views._available_manual_properties",
        lambda **_kwargs: [
            {
                "id": str(property_obj.pk),
                "name": "وحدة متاحة",
                "total_price": "1500.0000",
                "currency": "SAR",
                "nights": 2,
                "average_nightly_price": "750.0000",
            }
        ],
    )

    response = client.get(
        reverse("notifications:manual_booking_available_properties"),
        {
            "check_in": (timezone.localdate() + timedelta(days=10)).isoformat(),
            "check_out": (timezone.localdate() + timedelta(days=12)).isoformat(),
        },
    )

    assert response.status_code == 200
    assert response.json()["properties"][0]["total_price"] == "1500.0000"
    assert response.json()["properties"][0]["average_nightly_price"] == "750.0000"
