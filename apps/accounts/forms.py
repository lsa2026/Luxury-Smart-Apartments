"""Customer account forms with email-first authentication."""

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number

User = get_user_model()


class CustomerRegistrationForm(forms.ModelForm):
    first_name = forms.CharField(
        label=_("First name"),
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "given-name"}),
    )
    last_name = forms.CharField(
        label=_("Last name"),
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "family-name"}),
    )
    email = forms.EmailField(
        label=_("Email"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "inputmode": "email"}),
    )
    accept_terms = forms.BooleanField(
        label=_("I agree to the terms and privacy policy"),
        required=True,
    )

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email")

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip().casefold()
        if email == settings.OPERATIONS_OWNER_EMAIL:
            raise forms.ValidationError(
                _("This business address signs in through the secure owner verification route.")
            )
        if (
            User.objects.filter(email__iexact=email).exists()
            or User.objects.filter(username__iexact=email).exists()
        ):
            raise forms.ValidationError(_("An account already exists for this email."))
        return email

    def save(self, commit: bool = True):
        user = User(
            username=self.cleaned_data["email"],
            email=self.cleaned_data["email"],
            first_name=self.cleaned_data["first_name"].strip(),
            last_name=self.cleaned_data["last_name"].strip(),
        )
        # Email codes are the guest's credential. Keeping no local password
        # removes a field and a future reset journey from the booking flow.
        user.set_unusable_password()
        if commit:
            user.save()
        return user


class EmailCodeRequestForm(forms.Form):
    email = forms.EmailField(
        label=_("Email"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "inputmode": "email"}),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)

    def clean_email(self) -> str:
        return self.cleaned_data["email"].strip().casefold()


class CustomerAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(
        label=_("Email"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)
        self.fields["password"].widget.attrs["autocomplete"] = "current-password"

    def clean_username(self) -> str:
        return self.cleaned_data["username"].strip().casefold()


def _clean_profile_text(value: str) -> str:
    return " ".join(strip_tags(value).split()).strip()


class CustomerProfileForm(forms.Form):
    """The guest-controlled details shown beside their booking history.

    Email deliberately is not editable here. It is also the customer's sign-in
    credential and changing it must use the existing proof-by-email journey,
    rather than silently taking ownership of another mailbox.
    """

    first_name = forms.CharField(
        label=_("First name"),
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "given-name"}),
    )
    last_name = forms.CharField(
        label=_("Last name"),
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "family-name"}),
    )
    phone = forms.CharField(
        label=_("Phone number"),
        max_length=32,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "tel",
                "inputmode": "tel",
                "dir": "ltr",
                "placeholder": _("For example: +966 50 000 0000"),
            }
        ),
    )
    residence_address_line1 = forms.CharField(
        label=_("Residence address"),
        max_length=250,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "street-address"}),
    )
    residence_city = forms.CharField(
        label=_("City"),
        max_length=120,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "address-level2"}),
    )
    residence_region = forms.CharField(
        label=_("Region"),
        max_length=120,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "address-level1"}),
    )
    residence_postal_code = forms.CharField(
        label=_("Postal code"),
        max_length=32,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "postal-code", "dir": "ltr"}),
    )
    residence_country = forms.CharField(
        label=_("Country of residence"),
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "country-name"}),
    )

    def __init__(self, *args: object, user: object, profile: object, **kwargs: object) -> None:
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.update(
                {
                    "first_name": getattr(user, "first_name", ""),
                    "last_name": getattr(user, "last_name", ""),
                    "phone": getattr(profile, "phone", ""),
                    "residence_address_line1": getattr(profile, "residence_address_line1", ""),
                    "residence_city": getattr(profile, "residence_city", ""),
                    "residence_region": getattr(profile, "residence_region", ""),
                    "residence_postal_code": getattr(profile, "residence_postal_code", ""),
                    "residence_country": getattr(profile, "residence_country", ""),
                }
            )

    def clean_phone(self) -> str:
        value = _clean_profile_text(self.cleaned_data["phone"])
        if not value:
            return ""
        try:
            return normalize_phone_number(value)
        except InvalidPhoneNumber:
            raise forms.ValidationError(
                _("Enter a valid mobile number in international format, starting with +."),
            ) from None

    def clean(self) -> dict[str, str]:
        cleaned = super().clean()
        for field in (
            "first_name",
            "last_name",
            "residence_address_line1",
            "residence_city",
            "residence_region",
            "residence_postal_code",
            "residence_country",
        ):
            if field in cleaned:
                cleaned[field] = _clean_profile_text(cleaned[field])
        return cleaned


class EmailVerificationCodeForm(forms.Form):
    code = forms.CharField(
        max_length=6,
        min_length=6,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "pattern": "[0-9]*",
                "dir": "ltr",
            }
        ),
    )

    def clean_code(self) -> str:
        code = self.cleaned_data["code"].strip().replace(" ", "")
        if not code.isdigit() or len(code) != 6:
            raise forms.ValidationError(_("Enter the six-digit code from your email."))
        return code
