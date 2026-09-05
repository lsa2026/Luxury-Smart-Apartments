"""Explicit public forms for the local contact inbox."""

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
    phone = forms.CharField(
        label=_("Phone (optional)"),
        max_length=30,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "tel",
                "inputmode": "tel",
                "dir": "ltr",
                "placeholder": "+<country code> <number>",
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
        value = _clean_text(self.cleaned_data["phone"])
        if not value:
            return ""
        try:
            return normalize_phone_number(value)
        except InvalidPhoneNumber:
            raise forms.ValidationError(
                _("Enter a valid mobile number, with its country code if it is not a local number.")
            ) from None

    def clean_subject(self) -> str:
        return _clean_text(self.cleaned_data["subject"])

    def clean_message(self) -> str:
        value = _clean_text(self.cleaned_data["message"])
        if len(value) < 10:
            raise forms.ValidationError(_("Please provide a little more detail."))
        return value
