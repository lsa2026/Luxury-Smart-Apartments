"""Explicit public forms for local reservation modification requests."""

from django import forms


class ReasonMixin(forms.Form):
    reason = forms.CharField(
        label="سبب الطلب",
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 4}),
    )


class ExtensionRequestForm(ReasonMixin):
    new_check_out = forms.DateField(
        label="تاريخ المغادرة الجديد",
        widget=forms.DateInput(attrs={"type": "date"}),
    )


class DateChangeRequestForm(ReasonMixin):
    new_check_in = forms.DateField(
        label="تاريخ الوصول الجديد",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    new_check_out = forms.DateField(
        label="تاريخ المغادرة الجديد",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    new_guests = forms.IntegerField(label="عدد الضيوف", min_value=1)

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        check_in = cleaned.get("new_check_in")
        check_out = cleaned.get("new_check_out")
        if check_in and check_out and check_out <= check_in:
            self.add_error("new_check_out", "يجب أن تكون المغادرة بعد الوصول.")
        return cleaned


class GuestChangeRequestForm(ReasonMixin):
    new_guests = forms.IntegerField(label="عدد الضيوف الجديد", min_value=1)


class CancellationRequestForm(ReasonMixin):
    confirm = forms.BooleanField(
        label="أفهم أن هذا طلب مراجعة ولا يعني إلغاء الحجز أو استرداد المبلغ.",
        required=True,
    )
