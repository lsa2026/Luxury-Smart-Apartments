"""Local-only catalogue forms that never query the channel manager."""

from datetime import date

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class PropertyBrowseDatesForm(forms.Form):
    """Validate optional browse dates before carrying them to a property page."""

    check_in = forms.DateField(
        label=_("Check-in"),
        required=False,
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "dir": "ltr",
                "lang": "en-CA",
                "id": "filter-check-in",
            },
            format="%Y-%m-%d",
        ),
    )
    check_out = forms.DateField(
        label=_("Check-out"),
        required=False,
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "dir": "ltr",
                "lang": "en-CA",
                "id": "filter-check-out",
            },
            format="%Y-%m-%d",
        ),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        today = timezone.localdate().isoformat()
        self.fields["check_in"].widget.attrs["min"] = today
        self.fields["check_out"].widget.attrs["min"] = today

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        check_in = cleaned.get("check_in")
        check_out = cleaned.get("check_out")

        if bool(check_in) != bool(check_out):
            missing_field = "check_out" if check_in else "check_in"
            self.add_error(missing_field, _("Choose both check-in and check-out dates."))
            return cleaned

        if isinstance(check_in, date) and isinstance(check_out, date):
            if check_in < timezone.localdate():
                self.add_error("check_in", _("Check-in cannot be in the past."))
            if check_out <= check_in:
                self.add_error("check_out", _("Check-out must be after check-in."))
            elif (check_out - check_in).days > 366:
                self.add_error("check_out", _("The maximum stay is 366 nights."))

        return cleaned
