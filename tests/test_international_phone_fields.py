import pytest
from django.utils.translation import override

from apps.core.admin import SiteSettingAdminForm
from apps.core.forms import ContactForm
from apps.reservations.booking_forms import GuestDetailsForm

pytestmark = pytest.mark.django_db


def test_every_phone_input_prompts_for_an_international_country_code() -> None:
    expected_placeholder = "+<country code> <number>"
    expected_help = (
        "Enter an international number, starting with + and its country code, "
        "for example +966500000000."
    )

    with override("en"):
        assert ContactForm().fields["phone"].widget.attrs["placeholder"] == expected_placeholder
        assert str(ContactForm().fields["phone"].help_text) == expected_help
        assert (
            GuestDetailsForm().fields["guest_phone"].widget.attrs["placeholder"]
            == expected_placeholder
        )
        assert str(GuestDetailsForm().fields["guest_phone"].help_text) == expected_help
    assert (
        SiteSettingAdminForm().fields["contact_phone"].widget.attrs["placeholder"]
        == expected_placeholder
    )
    assert (
        SiteSettingAdminForm().fields["whatsapp_display_number"].widget.attrs["placeholder"]
        == expected_placeholder
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("+44 7911 123456", "+447911123456"),
        ("+1 (212) 555-1234", "+12125551234"),
    ],
)
def test_contact_form_accepts_and_normalizes_international_numbers(
    value: str,
    expected: str,
) -> None:
    form = ContactForm(
        {
            "name": "International guest",
            "email": "guest@example.invalid",
            "phone": value,
            "subject": "Question",
            "message": "Please contact me about my stay.",
            "website": "",
        }
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == expected


@pytest.mark.parametrize(
    "value", ["0500000000", "0033 6 12 34 56 78", "12345", "+9999999999999999"]
)
def test_contact_form_rejects_ambiguous_or_invalid_numbers(value: str) -> None:
    form = ContactForm(
        {
            "name": "International guest",
            "email": "guest@example.invalid",
            "phone": value,
            "subject": "Question",
            "message": "Please contact me about my stay.",
            "website": "",
        }
    )

    assert not form.is_valid()
    assert "phone" in form.errors


def test_contact_form_keeps_phone_optional() -> None:
    form = ContactForm(
        {
            "name": "International guest",
            "email": "guest@example.invalid",
            "phone": "",
            "subject": "Question",
            "message": "Please contact me about my stay.",
            "website": "",
        }
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == ""


def test_site_settings_phone_fields_use_the_same_international_format() -> None:
    form = SiteSettingAdminForm(
        data={
            "site_name": "Luxury Smart Apartments",
            "contact_phone": "+44 7911 123456",
            "whatsapp_display_number": "+33 6 12 34 56 78",
        }
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["contact_phone"] == "+447911123456"
    assert form.cleaned_data["whatsapp_display_number"] == "+33612345678"


def test_site_settings_reject_phone_numbers_without_an_international_country_code() -> None:
    form = SiteSettingAdminForm(
        data={
            "site_name": "Luxury Smart Apartments",
            "contact_phone": "0500000000",
            "whatsapp_display_number": "0033 6 12 34 56 78",
        }
    )

    assert not form.is_valid()
    assert {"contact_phone", "whatsapp_display_number"} <= set(form.errors)
