"""Explicit guest forms; no model mass assignment."""

import re
import secrets

from django import forms
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _


def _clean_text(value: str) -> str:
    without_markup = strip_tags(value)
    return "".join(character for character in without_markup if character.isprintable()).strip()


class GuestDetailsForm(forms.Form):
    guest_first_name = forms.CharField(label=_("First name"), max_length=100)
    guest_last_name = forms.CharField(label=_("Last name"), max_length=100)
    guest_email = forms.EmailField(label=_("Email address"), max_length=254)
    guest_phone = forms.CharField(
        label=_("Phone number"),
        max_length=20,
        help_text=_("Use international format, for example +9665XXXXXXXX."),
    )
    guest_country_code = forms.CharField(label=_("Country code"), min_length=2, max_length=2)
    special_requests = forms.CharField(
        label=_("Special requests"),
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    terms_accepted = forms.BooleanField(label=_("I accept the terms"))
    privacy_accepted = forms.BooleanField(label=_("I accept the privacy policy"))
    marketing_consent = forms.BooleanField(
        label=_("I would like to receive marketing offers"),
        required=False,
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput(), max_length=64)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["idempotency_key"] = secrets.token_urlsafe(32)

    def clean_guest_first_name(self) -> str:
        value = _clean_text(self.cleaned_data["guest_first_name"])
        if not value:
            raise forms.ValidationError(_("First name is required."))
        return value

    def clean_guest_last_name(self) -> str:
        value = _clean_text(self.cleaned_data["guest_last_name"])
        if not value:
            raise forms.ValidationError(_("Last name is required."))
        return value

    def clean_guest_phone(self) -> str:
        raw = self.cleaned_data["guest_phone"]
        normalized = re.sub(r"[\s().-]", "", raw)
        if not re.fullmatch(r"\+[1-9]\d{7,14}", normalized):
            raise forms.ValidationError(_("Enter a valid international number beginning with +."))
        return normalized

    def clean_guest_country_code(self) -> str:
        value = self.cleaned_data["guest_country_code"].upper()
        if not re.fullmatch(r"[A-Z]{2}", value):
            raise forms.ValidationError(_("Country code must contain two letters."))
        return value

    def clean_special_requests(self) -> str:
        return _clean_text(self.cleaned_data["special_requests"])

    def clean_idempotency_key(self) -> str:
        value = self.cleaned_data["idempotency_key"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", value):
            raise forms.ValidationError(_("The request identifier is invalid."))
        return value
