"""Customer account forms with email-first authentication."""

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.utils.translation import gettext_lazy as _

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
