"""Explicit public forms for local reservation modification requests."""

from django import forms
from django.utils.translation import gettext_lazy as _


class ReasonMixin(forms.Form):
    reason = forms.CharField(
        label=_("Reason for the request"),
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 4}),
    )


class ExtensionRequestForm(ReasonMixin):
    new_check_out = forms.DateField(
        label=_("New check-out date"),
        widget=forms.DateInput(attrs={"type": "date"}),
    )


class DateChangeRequestForm(ReasonMixin):
    new_check_in = forms.DateField(
        label=_("New check-in date"),
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    new_check_out = forms.DateField(
        label=_("New check-out date"),
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    new_guests = forms.IntegerField(label=_("Number of guests"), min_value=1)

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        check_in = cleaned.get("new_check_in")
        check_out = cleaned.get("new_check_out")
        if check_in and check_out and check_out <= check_in:
            self.add_error("new_check_out", _("Check-out must be after check-in."))
        return cleaned


class GuestChangeRequestForm(ReasonMixin):
    new_guests = forms.IntegerField(label=_("New number of guests"), min_value=1)


class CancellationRequestForm(ReasonMixin):
    confirm = forms.BooleanField(
        label=_(
            "I understand this is a review request and does not cancel "
            "the booking or issue a refund."
        ),
        required=True,
    )
