"""No form accepts an award base, credit amount or a guest-controlled terms version."""

from decimal import Decimal

from django import forms

from .copy import copy


class JoinForm(forms.Form):
    consent = forms.BooleanField()
    confirm_inviter = forms.BooleanField(required=False)

    def __init__(self, *args, has_inviter=False, **kwargs):
        super().__init__(*args, **kwargs)
        labels = copy()
        self.fields["consent"].label = labels["consent"]
        self.fields["confirm_inviter"].label = labels["inviter"]
        self.fields["confirm_inviter"].required = has_inviter
        if not has_inviter:
            del self.fields["confirm_inviter"]
        for field in self.fields.values():
            field.error_messages["required"] = labels["invalid"]


class TransferForm(forms.Form):
    recipient = forms.CharField(max_length=32)
    amount = forms.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.01"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        labels = copy()
        self.fields["recipient"].label = labels["recipient"]
        self.fields["amount"].label = labels["amount"]
        self.fields["recipient"].widget.attrs.update(autocomplete="off", dir="ltr")
        self.fields["amount"].widget.attrs.update(inputmode="decimal", step="0.01")
        for field in self.fields.values():
            for code in (
                "required",
                "invalid",
                "min_value",
                "max_digits",
                "max_decimal_places",
                "max_whole_digits",
            ):
                field.error_messages[code] = labels["invalid"]
