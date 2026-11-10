"""Management-collected billing, fake providers and a disposable database only."""

from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.guest_documents import documents_digest
from apps.payments.hyperpay.service import HyperPayService
from apps.payments.models import PaymentAttempt
from apps.payments.payment_link_views import PaymentLinkBillingForm, saved_manual_billing
from apps.payments.payment_links import issue_request
from apps.reservations.booking_forms import BillingAddressForm
from apps.reservations.manual_bookings import (
    create_manual_booking_in_hostaway,
    finalize_manual_booking_draft,
)
from apps.reservations.models import BookingIntent, ManualBookingDraft, Reservation
from apps.reservations.operations_forms import ManualBookingFinalizeForm
from tests.test_checkout_address_places import isolated_places as isolated_places
from tests.test_hyperpay import HyperPayStub
from tests.test_manual_booking_operations_phase61 import FakeManualHostawayBookingService
from tests.test_manual_guest_language import guest_fields, make_draft
from tests.test_reservation_payment_links import (
    billing,
    manual_stay,
    page_url,
    remote,
)
from tests.test_reservation_payment_links import isolated_settings as isolated_settings

pytestmark = pytest.mark.django_db


def addressed_stay(language="ar"):
    stay = manual_stay(language=language)
    ManualBookingDraft.objects.filter(quote=stay.booking_intent.quote).update(
        billing_address=billing()
    )
    for name, value in billing().items():
        setattr(stay.booking_intent, name, value)
    stay.booking_intent.save()
    return stay


def consents():
    return {
        "terms_accepted": "on",
        "house_rules_accepted": "on",
        "documents_digest": documents_digest(),
    }


@pytest.mark.usefixtures("isolated_places")
def test_admin_collects_address_and_carries_it_to_the_payment_intent():
    draft = make_draft()
    client = Client()
    client.force_login(draft.created_by)
    url = reverse("notifications:manual_booking_detail", args=[draft.pk])
    page = client.get(url)
    assert page.status_code == 200
    assert page.context["address_places_enabled"]
    assert page.content.count(b"data-checkout-address hidden") == 1
    assert b"js/checkout-address.js" in page.content
    assert "/morning-address/suggestions/" in page.context["address_suggestions_url"]
    assert Client().get(url).status_code in {302, 403}
    assert client.post(url, guest_fields(draft)).status_code == 302
    draft.refresh_from_db()
    assert draft.billing_address == billing()
    result = create_manual_booking_in_hostaway(
        draft_id=draft.pk, booking_service=FakeManualHostawayBookingService()
    )
    assert result.code == "created"
    intent = BookingIntent.objects.get()
    for name, value in billing().items():
        assert getattr(intent, name) == value
    assert not intent.house_rules_accepted_at
    assert not intent.legal_acceptance
    assert not PaymentAttempt.objects.exists()


@pytest.mark.parametrize("field", list(billing()))
def test_admin_must_complete_address_before_guest_data_can_be_finalized(field):
    draft = make_draft()
    data = guest_fields(draft)
    data.pop(field)
    form = ManualBookingFinalizeForm(data, draft=draft, require_address=True)
    assert not form.is_valid() and field in form.errors
    draft.refresh_from_db()
    assert draft.status == "quoted" and draft.billing_address == {}
    assert not Reservation.objects.exists()


def test_direct_finalization_rejects_invalid_provided_address_without_writes():
    draft = make_draft()
    outcome = finalize_manual_booking_draft(
        draft_id=draft.pk,
        guest_data=guest_fields(draft) | {"billing_address": {"billing_country": "ZZ"}},
        final_total_price=draft.final_total_price,
    )
    assert outcome.code == "invalid_billing_address"
    draft.refresh_from_db()
    assert draft.billing_address == {} and draft.status == "quoted"


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_payment_link_shows_address_and_keeps_consents_unchecked(language):
    stay = addressed_stay(language)
    request = issue_request(stay.pk)
    page = Client().get(page_url(request))
    assert page.status_code == 200
    assert page.context["saved_billing"] == billing()
    form = page.context["form"]
    for name, value in billing().items():
        assert form[name].value() == value
    assert not form["terms_accepted"].value()
    assert not form["house_rules_accepted"].value()
    assert "<details><summary>" in page.content.decode()
    assert page["Cache-Control"] == "no-store, private"
    assert not PaymentAttempt.objects.exists()


def test_guest_only_accepts_terms_and_pays_using_saved_address():
    stay = addressed_stay()
    request = issue_request(stay.pk)
    stub, hostaway = HyperPayStub(), remote(stay)
    with (
        patch("apps.payments.payment_links.HostawayClient") as cls,
        patch(
            "apps.payments.payment_links.HyperPayService", return_value=HyperPayService(client=stub)
        ),
    ):
        cls.return_value.__enter__.return_value = hostaway
        page = Client().post(page_url(request), consents())
    assert page.status_code == 200 and b"paymentWidgets.js" in page.content
    stay.booking_intent.refresh_from_db()
    assert stay.booking_intent.house_rules_accepted_at
    for name, value in billing().items():
        assert getattr(stay.booking_intent, name) == value
    assert stub.payload["billing.city"] == "Riyadh"
    assert PaymentAttempt.objects.count() == 1


@pytest.mark.parametrize("field", ["terms_accepted", "house_rules_accepted", "documents_digest"])
def test_saved_address_does_not_accept_terms_for_the_guest(field):
    request = issue_request(addressed_stay().pk)
    data = consents()
    data.pop(field)
    page = Client().post(page_url(request), data)
    assert page.status_code == 200 and field in page.context["form"].errors
    assert not PaymentAttempt.objects.exists()
    request.refresh_from_db()
    assert not request.accepted_at


def test_guest_can_correct_address_but_cannot_submit_a_blank_required_field():
    request = issue_request(addressed_stay().pk)
    page = Client().post(page_url(request), consents() | {"billing_city": ""})
    assert page.status_code == 200 and "billing_city" in page.context["form"].errors
    assert not PaymentAttempt.objects.exists()


def test_legacy_draft_does_not_treat_placeholder_address_as_collected():
    stay = manual_stay()
    request = issue_request(stay.pk)
    assert saved_manual_billing(request) is None
    page = Client().post(page_url(request), consents())
    assert page.status_code == 200 and "billing_city" in page.context["form"].errors
    assert not PaymentAttempt.objects.exists()


@pytest.mark.parametrize(
    "name,value,valid",
    [
        ("billing_street1", "12345", False),
        ("billing_street1", "شارع ١٠", True),
        ("billing_street1", "X" * 100, True),
        ("billing_street1", "X" * 101, False),
        ("billing_city", "الرياض", True),
        ("billing_city", "X" * 81, False),
        ("billing_state", "Riyadh Province", False),
        ("billing_state", "الرياض", False),
        ("billing_state", "Riyadh-01", False),
        ("billing_state", "Riyadh.01", True),
        ("billing_state", "X" * 51, False),
        ("billing_postcode", "SW1A 1AA", False),
        ("billing_postcode", "10001-1234", False),
        ("billing_postcode", "١٢٣٤٥", False),
        ("billing_postcode", "X" * 16, True),
        ("billing_postcode", "X" * 17, False),
        ("billing_country", "ZZ", False),
    ],
)
def test_admin_and_prepared_payment_use_the_approved_billing_contract(name, value, valid):
    data = billing() | {name: value}
    address = BillingAddressForm(data)
    payment = PaymentLinkBillingForm(data | consents(), approved_address=True)
    assert address.is_valid() is valid
    assert payment.is_valid() is valid
    if not valid:
        assert name in address.errors and name in payment.errors


@pytest.mark.usefixtures("isolated_places")
def test_admin_address_rules_and_search_stay_arabic_even_with_english_browser():
    draft = make_draft()
    client = Client()
    client.force_login(draft.created_by)
    page = client.get(
        reverse("notifications:manual_booking_detail", args=[draft.pk]),
        HTTP_ACCEPT_LANGUAGE="en",
    )
    content = page.content.decode()
    assert "عنوان الشارع" in content and "من ١ إلى ١٠٠ حرف" in content
    assert "دون مسافات" in content and "Riyadh" in content
    assert "Fill in your address easily" not in content
    assert "عبّئ عنوانك بسهولة" in content
    assert b"data-morning-validation" in page.content
    assert b"js/morning-validation.js" in page.content
    assert b"data-morning-pattern" in page.content


def test_admin_cannot_save_a_region_with_spaces_from_address_suggestions():
    draft = make_draft()
    client = Client()
    client.force_login(draft.created_by)
    page = client.post(
        reverse("notifications:manual_booking_detail", args=[draft.pk]),
        guest_fields(draft) | {"billing_state": "Riyadh Province"},
    )
    assert page.status_code == 200 and "billing_state" in page.context["form"].errors
    assert "دون مسافات" in page.content.decode()
    draft.refresh_from_db()
    assert draft.status == "quoted" and draft.billing_address == {}


def test_previously_saved_invalid_region_must_be_corrected_before_payment():
    stay = addressed_stay()
    stay.booking_intent.billing_state = "Riyadh Province"
    stay.booking_intent.save(update_fields=["billing_state"])
    ManualBookingDraft.objects.filter(quote=stay.booking_intent.quote).update(
        billing_address=billing() | {"billing_state": "Riyadh Province"}
    )
    request = issue_request(stay.pk)
    page = Client().get(page_url(request))
    assert "billing_state" in page.context["address_errors"]
    assert "يرجى تصحيح عنوان الفوترة المحفوظ" in page.content.decode()
    page = Client().post(page_url(request), consents())
    assert "billing_state" in page.context["form"].errors
    assert not PaymentAttempt.objects.exists()
    request.refresh_from_db()
    assert not request.accepted_at


def test_legacy_payment_form_retains_its_existing_address_contract():
    form = PaymentLinkBillingForm(
        billing()
        | consents()
        | {"billing_state": "Riyadh Province", "billing_postcode": "SW1A 1AA"}
    )
    assert form.is_valid(), form.errors
