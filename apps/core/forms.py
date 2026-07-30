"""Explicit public forms for the local contact inbox."""

from django import forms
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _


def _clean_text(value: str) -> str:
    return " ".join(strip_tags(value).split()).strip()


class ContactForm(forms.Form):
    name = forms.CharField(label=_("Name"), max_length=150)
    email = forms.EmailField(label=_("Email address"), max_length=254)
    phone = forms.CharField(label=_("Phone (optional)"), max_length=30, required=False)
    subject = forms.CharField(label=_("Subject"), max_length=200)
    message = forms.CharField(
        label=_("Message"),
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 7}),
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
        return _clean_text(self.cleaned_data["phone"])

    def clean_subject(self) -> str:
        return _clean_text(self.cleaned_data["subject"])

    def clean_message(self) -> str:
        value = _clean_text(self.cleaned_data["message"])
        if len(value) < 10:
            raise forms.ValidationError(_("Please provide a little more detail."))
        return value
