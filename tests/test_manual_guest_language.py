"""Owner-selected language is independent of the Arabic administration UI."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.translation import override

from apps.reservations.manual_bookings import (
    create_manual_booking_draft,
    create_manual_booking_in_hostaway,
    finalize_manual_booking_draft,
    recheck_manual_booking_draft,
)
from apps.reservations.models import BookingIntent, ManualBookingDraft, Reservation
from apps.reservations.operations_forms import ManualBookingFinalizeForm
from apps.reservations.services.hostaway_booking import HostawayBookingService
from tests.test_booking_models_services import make_availability, make_property
from tests.test_hostaway_booking_phase5 import complete_availability, create_result
from tests.test_manual_booking_operations_phase61 import FakeAvailabilityService, owner

pytestmark = pytest.mark.django_db


def make_draft():
    property_obj = make_property()
    property_obj.hostaway_listing_map_id = 9001
    property_obj.save(update_fields=["hostaway_listing_map_id"])
    available = make_availability(property_obj)
    draft = create_manual_booking_draft(
        property_obj=property_obj,
        check_in=available.quote.check_in,
        check_out=available.quote.check_out,
        guests=1,
        actor=owner(),
        availability_service=FakeAvailabilityService(available),
    ).draft
    assert draft is not None
    return draft


def guest_fields(draft, language="ar"):
    return {
        "guest_first_name": "Test",
        "guest_last_name": "Guest",
        "guest_email": "guest@example.invalid",
        "guest_phone": "+966501234567",
        "guest_language": language,
        "final_total_price": draft.final_total_price,
    }


class CaptureClient:
    def __init__(self):
        self.payloads = []

    def create_reservation_with_price_details(self, request):
        self.payloads.append(request.to_payload())
        return create_result(Reservation.objects.get(), payment_status="unpaid")


class RevalidatedAvailability:
    def check(self, request, *, bypass_cache):
        assert bypass_cache
        return complete_availability(BookingIntent.objects.get())


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@override_settings(HOSTAWAY_LIVE_BOOKING_ENABLED=True, HOSTAWAY_DIRECT_CHANNEL_ID=2000)
def test_arabic_admin_selection_reaches_hostaway_without_a_payment(language):
    draft = make_draft()
    client = Client()
    client.force_login(draft.created_by)
    with override("ar"):
        response = client.post(
            reverse("notifications:manual_booking_detail", args=[draft.pk]),
            guest_fields(draft, language),
        )
    assert response.status_code == 302
    draft.refresh_from_db()
    assert draft.guest_language == language
    assert draft.status == ManualBookingDraft.Status.READY_FOR_PAYMENT
    assert not Reservation.objects.exists()

    provider = CaptureClient()
    service = HostawayBookingService(
        client=provider, availability_service=RevalidatedAvailability()
    )
    result = create_manual_booking_in_hostaway(draft_id=draft.pk, booking_service=service)
    assert result.code == "created"
    assert provider.payloads[0]["guestLocale"] == language
    intent = BookingIntent.objects.get()
    assert intent.language == language
    assert intent.total_price == draft.final_total_price
    assert result.reservation.payment_status == "unpaid"
    repeated = create_manual_booking_in_hostaway(draft_id=draft.pk, booking_service=service)
    assert repeated.code == "already_created"
    assert len(provider.payloads) == 1
    assert BookingIntent.objects.get().language == language


def test_language_choices_default_arabic_and_preserve_selection_on_price_error():
    draft = make_draft()
    assert draft.guest_language == ""
    form = ManualBookingFinalizeForm(draft=draft)
    assert form["guest_language"].value() == "ar"
    assert list(form.fields["guest_language"].choices) == [
        ("ar", "العربية"),
        ("en", "الإنجليزية"),
        ("fr", "الفرنسية"),
    ]
    data = guest_fields(draft, "fr")
    data["final_total_price"] = "-1"
    invalid = ManualBookingFinalizeForm(data, draft=draft)
    assert not invalid.is_valid()
    assert invalid["guest_language"].value() == "fr"


@pytest.mark.parametrize("language", ["de", "fr-FR", "<script>"])
def test_invalid_language_is_rejected_without_writing_a_draft(language):
    draft = make_draft()
    form = ManualBookingFinalizeForm(guest_fields(draft, language), draft=draft)
    assert not form.is_valid()
    assert "guest_language" in form.errors
    with pytest.raises(ValidationError):
        finalize_manual_booking_draft(
            draft_id=draft.pk,
            guest_data=guest_fields(draft, language),
            final_total_price=draft.final_total_price,
        )
    draft.refresh_from_db()
    assert draft.guest_language == ""
    assert draft.status == ManualBookingDraft.Status.QUOTED


def test_already_open_legacy_form_defaults_to_arabic_and_recheck_preserves_selection():
    draft = make_draft()
    data = guest_fields(draft)
    del data["guest_language"]
    form = ManualBookingFinalizeForm(data, draft=draft)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_language"] == "ar"
    finalize_manual_booking_draft(
        draft_id=draft.pk,
        guest_data=guest_fields(draft, "fr"),
        final_total_price=draft.final_total_price,
    )
    rechecked = recheck_manual_booking_draft(
        draft_id=draft.pk,
        actor=draft.created_by,
        availability_service=FakeAvailabilityService(make_availability(draft.property)),
    )
    assert rechecked.draft.guest_language == "fr"
    assert ManualBookingFinalizeForm(draft=rechecked.draft)["guest_language"].value() == "fr"


def test_admin_shows_language_in_form_and_on_confirmation():
    draft = make_draft()
    client = Client()
    client.force_login(draft.created_by)
    url = reverse("notifications:manual_booking_detail", args=[draft.pk])
    before = client.get(url).content.decode()
    assert 'name="guest_language"' in before
    assert "لغة الضيف" in before
    assert "lsa-manual-booking__language-select" in before
    assert 'value="ar" selected' in before
    finalize_manual_booking_draft(
        draft_id=draft.pk,
        guest_data=guest_fields(draft, "en"),
        final_total_price=Decimal(draft.final_total_price),
    )
    after = client.get(url).content.decode()
    assert "لغة الضيف:</b> الإنجليزية" in after
    assert "تأكيد الحجز في Hostaway وإرسال الطلب للمحاسبة" in after
