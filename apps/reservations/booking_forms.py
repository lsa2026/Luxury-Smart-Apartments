"""Explicit guest forms; no model mass assignment."""

import re
import secrets

import phonenumbers
from django import forms
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _

from apps.payments.countries import ISO_ALPHA2_COUNTRY_CODES, normalize_country_code
from apps.payments.regions import SAUDI_COUNTRY_CODE, region_choices

# Used when the guest has not chosen a billing country yet, and as the last
# reading of a bare national number.
_FALLBACK_REGION = "SA"


def _clean_text(value: str) -> str:
    without_markup = strip_tags(value)
    return "".join(character for character in without_markup if character.isprintable()).strip()


def _normalize_phone_number(raw: str, default_country_code: str) -> str:
    """Return the number in E.164, accepting any country's local format.

    The number is read against several candidate regions in turn. An explicit
    international prefix is authoritative and needs no region. Otherwise the
    country the guest selected is tried first, then the site default, so a
    guest with an overseas billing address and a Saudi mobile is still accepted
    rather than rejected for the mismatch.
    """
    stripped = re.sub(r"[^\d+]", "", _clean_text(raw))
    if stripped.startswith("00"):
        stripped = f"+{stripped[2:]}"

    regions: list[str | None] = [None] if stripped.startswith("+") else []
    for candidate in (default_country_code.upper(), _FALLBACK_REGION):
        if candidate in ISO_ALPHA2_COUNTRY_CODES and candidate not in regions:
            regions.append(candidate)

    for region in regions:
        try:
            parsed = phonenumbers.parse(stripped, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

    raise forms.ValidationError(
        _("Enter a valid mobile number, with its country code if it is not a local number.")
    )


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
            "Any country is accepted. Write it with a country code, such as "
            "+966500000000, or as a local number for the country you select below."
        ),
        widget=forms.TextInput(
            attrs={
                "autocomplete": "tel",
                "dir": "ltr",
                "inputmode": "tel",
                "placeholder": "+966 50 000 0000",
            }
        ),
    )
    # Although /v1/checkouts accepts its four core fields, the provider's 3-D
    # Secure 2 contract makes the street and city mandatory for authentication.
    # The postcode is not mandatory and therefore stays optional.
    billing_street1 = forms.CharField(
        label=_("Street address"),
        max_length=100,
        widget=forms.TextInput(attrs={"autocomplete": "address-line1"}),
    )
    billing_city = forms.CharField(
        label=_("City"),
        max_length=80,
        widget=forms.TextInput(attrs={"autocomplete": "address-level2"}),
    )
    # Saudi guests pick from the thirteen regions; everyone else types their own.
    billing_state = forms.CharField(
        label=_("State or region"),
        max_length=50,
        widget=forms.TextInput(attrs={"autocomplete": "address-level1"}),
    )
    billing_region_sa = forms.ChoiceField(
        label=_("State or region"),
        choices=(),
        required=False,
        widget=forms.Select(attrs={"autocomplete": "address-level1", "data-region-select": ""}),
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
        self.fields["billing_region_sa"].choices = region_choices()
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
        self._resolve_region(cleaned)
        phone = cleaned.get("guest_phone")
        if isinstance(phone, str):
            country = cleaned.get("billing_country")
            country_code = country if isinstance(country, str) else self.default_country_code
            try:
                cleaned["guest_phone"] = _normalize_phone_number(phone, country_code)
            except forms.ValidationError as exc:
                self.add_error("guest_phone", exc)
        return cleaned

    def _resolve_region(self, cleaned: dict[str, object]) -> None:
        """Take the region from the select for Saudi Arabia, the text elsewhere.

        Only one of the two ever reaches the gateway, so a guest who starts in
        one country and switches cannot leave a stale value behind.
        """
        country = cleaned.get("billing_country")
        if country == SAUDI_COUNTRY_CODE:
            chosen = (cleaned.get("billing_region_sa") or "").strip()
            # Either control satisfies the field. The select is what the page
            # shows, but accepting the text keeps older clients and direct posts
            # working rather than failing them on presentation.
            if chosen:
                cleaned["billing_state"] = chosen
                self.errors.pop("billing_state", None)
                return
            if cleaned.get("billing_state"):
                return
            self.add_error("billing_region_sa", _("Choose your region."))
            return
        cleaned["billing_region_sa"] = ""
        if not cleaned.get("billing_state"):
            self.add_error("billing_state", _("This field is required."))

    def clean_special_requests(self) -> str:
        return _clean_text(self.cleaned_data["special_requests"])

    def clean_idempotency_key(self) -> str:
        value = self.cleaned_data["idempotency_key"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", value):
            raise forms.ValidationError(_("The request identifier is invalid."))
        return value
