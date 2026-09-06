"""Explicit guest forms; no model mass assignment."""

import re
import secrets

from django import forms
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.payments.countries import ISO_ALPHA2_COUNTRY_CODES, normalize_country_code


def _clean_text(value: str) -> str:
    without_markup = strip_tags(value)
    return "".join(character for character in without_markup if character.isprintable()).strip()


class GuestDetailsForm(forms.Form):
    guest_first_name = forms.CharField(
        label=_("First name"),
        max_length=100,
        widget=forms.TextInput(attrs={"autocomplete": "given-name"}),
    )
    guest_last_name = forms.CharField(
        label=_("Last name"),
        max_length=100,
        widget=forms.TextInput(attrs={"autocomplete": "family-name"}),
    )
    guest_email = forms.EmailField(
        label=_("Email address"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "inputmode": "email"}),
    )
    guest_phone = forms.CharField(
        label=_("Phone number"),
        # Long enough for an international number written with spaces,
        # brackets or dashes; the value is normalised to E.164 on clean.
        max_length=32,
        help_text=_(
            "Enter an international number, starting with + and its country code, "
            "for example +966500000000."
        ),
        widget=forms.TextInput(
            attrs={
                "autocomplete": "tel",
                "dir": "ltr",
                "inputmode": "tel",
                "placeholder": "+<country code> <number>",
            }
        ),
    )
    # Optional by decision: /v1/checkouts requires only entityId, amount,
    # currency and paymentType. The address still helps 3-D Secure risk scoring,
    # so it is asked for and sent when given, never demanded.
    billing_street1 = forms.CharField(
        label=_("Street address"),
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "address-line1"}),
    )
    billing_city = forms.CharField(
        label=_("City"),
        max_length=80,
        widget=forms.TextInput(attrs={"autocomplete": "address-level2"}),
    )
    billing_state = forms.CharField(
        label=_("State or region"),
        max_length=50,
        widget=forms.TextInput(attrs={"autocomplete": "address-level1"}),
    )
    billing_country = forms.ChoiceField(
        label=_("Country"),
        choices=(),
        widget=forms.Select(
            attrs={
                "autocomplete": "country",
                "data-country-select": "",
            }
        ),
    )
    billing_postcode = forms.RegexField(
        label=_("Postal code"),
        regex=r"^[A-Za-z0-9]{1,16}$",
        max_length=16,
        required=False,
        widget=forms.TextInput(
            attrs={"autocomplete": "postal-code", "dir": "ltr", "inputmode": "text"}
        ),
    )
    special_requests = forms.CharField(
        label=_("Special requests"),
        max_length=1000,
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": _("Optional: arrival time, accessibility, or stay preferences"),
            }
        ),
    )
    terms_accepted = forms.BooleanField(label=_("I accept the terms"))
    privacy_accepted = forms.BooleanField(label=_("I accept the privacy policy"))
    marketing_consent = forms.BooleanField(
        label=_("I would like to receive marketing offers"),
        required=False,
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput(), max_length=64)

    def __init__(
        self,
        *args: object,
        default_country_code: str = "SA",
        **kwargs: object,
    ) -> None:
        self.default_country_code = default_country_code.upper()
        super().__init__(*args, **kwargs)
        self.fields["billing_country"].choices = [
            ("", _("Choose your country")),
            *((code, code) for code in sorted(ISO_ALPHA2_COUNTRY_CODES)),
        ]
        if not self.is_bound:
            self.initial["idempotency_key"] = secrets.token_urlsafe(32)
            self.initial["billing_country"] = self.default_country_code

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
        return _clean_text(self.cleaned_data["guest_phone"])

    def clean_billing_country(self) -> str:
        try:
            return normalize_country_code(self.cleaned_data["billing_country"])
        except ValueError as exc:
            raise forms.ValidationError(_("Enter a valid ISO two-letter country code.")) from exc

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        for name in ("billing_street1", "billing_city", "billing_state"):
            value = cleaned.get(name)
            if isinstance(value, str):
                cleaned[name] = _clean_text(value)
        if not cleaned.get("billing_city"):
            self.add_error("billing_city", _("This field is required."))
        phone = cleaned.get("guest_phone")
        if isinstance(phone, str):
            try:
                cleaned["guest_phone"] = normalize_phone_number(phone)
            except InvalidPhoneNumber:
                self.add_error(
                    "guest_phone",
                    _(
                        "Enter a valid mobile number in international format, starting "
                        "with +, for example +966500000000."
                    ),
                )
        return cleaned

    def clean_special_requests(self) -> str:
        return _clean_text(self.cleaned_data["special_requests"])

    def clean_idempotency_key(self) -> str:
        value = self.cleaned_data["idempotency_key"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", value):
            raise forms.ValidationError(_("The request identifier is invalid."))
        return value
