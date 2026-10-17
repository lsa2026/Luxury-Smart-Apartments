"""Explicit public forms for the local contact inbox."""

import phonenumbers
from django import forms
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _

from .phone_numbers import InvalidPhoneNumber, normalize_phone_number


def _clean_text(value: str) -> str:
    return " ".join(strip_tags(value).split()).strip()


class ContactForm(forms.Form):
    name = forms.CharField(
        label=_("Name"),
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )
    email = forms.EmailField(
        label=_("Email address"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "inputmode": "email"}),
    )
    phone_country = forms.ChoiceField(
        label=_("Phone number country"),
        required=False,
        initial="SA",
        choices=[
            (code, f"{code} (+{phonenumbers.country_code_for_region(code)})")
            for code in ["SA", *sorted(phonenumbers.SUPPORTED_REGIONS - {"SA"})]
        ],
        widget=forms.Select(attrs={"data-contact-phone-country": ""}),
    )
    phone = forms.CharField(
        label=_("Phone (optional)"),
        max_length=64,
        required=False,
        help_text=_(
            "Choose your phone number's country, then enter your usual number, "
            "including the starting zero if needed. You can also paste a full international number."
        ),
        widget=forms.TextInput(
            attrs={
                "autocomplete": "tel",
                "inputmode": "tel",
                "dir": "ltr",
                "placeholder": "0501234567",
                "aria-describedby": "contact-phone-help",
            }
        ),
    )
    subject = forms.CharField(
        label=_("Subject"),
        max_length=200,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    message = forms.CharField(
        label=_("Message"),
        max_length=2000,
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": _("Include your booking number if your message is about a stay."),
            }
        ),
    )
    website = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "tabindex": "-1",
                "autocomplete": "off",
                "aria-hidden": "true",
            }
        ),
    )

    def clean_website(self) -> str:
        value = self.cleaned_data["website"]
        if value:
            raise forms.ValidationError(_("Unable to send this message."))
        return ""

    def clean_name(self) -> str:
        return _clean_text(self.cleaned_data["name"])

    def clean_phone(self) -> str:
        value = "".join(
            char for char in _clean_text(self.cleaned_data["phone"]) if char.isprintable()
        )
        if not value:
            return ""
        try:
            return normalize_phone_number(
                value, default_region=self.cleaned_data.get("phone_country") or "SA"
            )
        except InvalidPhoneNumber:
            raise forms.ValidationError(
                _("Check your phone number and the selected country.")
            ) from None

    def clean_subject(self) -> str:
        return _clean_text(self.cleaned_data["subject"])

    def clean_message(self) -> str:
        value = _clean_text(self.cleaned_data["message"])
        if len(value) < 10:
            raise forms.ValidationError(_("Please provide a little more detail."))
        return value
