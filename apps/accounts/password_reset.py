"""Password reset wired into the site's own email queue.

Django's stock form calls ``send_mail`` itself. Overriding ``save`` keeps the
built-in token, validation and view flow while routing the message through the
same queue as every other email, so a reset inherits retries, provider logging
and recipient masking.
"""

from typing import Any

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from .emails import queue_password_reset_email

User = get_user_model()


class QueuedPasswordResetForm(PasswordResetForm):
    email = forms.EmailField(
        label=_("Email"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "inputmode": "email"}),
    )

    def get_users(self, email: str) -> Any:
        """Only accounts that can actually sign in are worth a reset link."""
        return User.objects.filter(
            email__iexact=email,
            is_active=True,
        ).exclude(password="")

    def save(self, **kwargs: Any) -> None:
        del kwargs
        language = (get_language() or "ar").split("-")[0]
        for user in self.get_users(self.cleaned_data["email"]):
            queue_password_reset_email(user, language=language)


class LuxurySetPasswordForm(SetPasswordForm):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].label = _("New password")
        self.fields["new_password2"].label = _("Confirm the new password")
        for name in ("new_password1", "new_password2"):
            self.fields[name].widget.attrs["autocomplete"] = "new-password"
