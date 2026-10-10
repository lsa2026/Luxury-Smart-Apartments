"""Bounded owner input; imported values are never trusted as HTML or headers."""

import re

from django import forms

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number


def plain(value):
    return re.sub(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", "", str(value or "")).strip()


def invoice_phone(value):
    value = value.strip()
    if value.startswith("00"):
        value = "+" + value[2:]
    return normalize_phone_number(value)


class InvoiceLookupForm(forms.Form):
    hostaway_id = forms.IntegerField(
        label="رقم الحجز في Hostaway",
        min_value=1,
        max_value=9223372036854775807,
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
    )


class InvoiceReviewForm(forms.Form):
    snapshot_token = forms.CharField(widget=forms.HiddenInput, max_length=64)
    platform_reference = forms.CharField(label="رقم حجز المنصة الظاهر للضيف", max_length=100)
    guest_name = forms.CharField(label="اسم الضيف", max_length=160)
    guest_email = forms.EmailField(label="البريد الإلكتروني", required=False)
    guest_phone = forms.CharField(label="الجوال بالصيغة الدولية", max_length=30, required=False)
    language = forms.ChoiceField(
        label="لغة الفاتورة",
        choices=[("ar", "العربية"), ("en", "الإنجليزية"), ("fr", "الفرنسية")],
    )
    confirm_details = forms.BooleanField(
        label="راجعت بيانات الضيف ورقم حجز المنصة ومبلغ الإقامة وأعتمد إصدار الفاتورة."
    )

    def clean_platform_reference(self):
        value = plain(self.cleaned_data["platform_reference"])
        if not re.fullmatch(r"[\w .:/\-]{1,100}", value):
            raise forms.ValidationError("أدخل رقم حجز المنصة الصحيح دون رموز غير مناسبة.")
        return value

    def clean_guest_name(self):
        value = plain(self.cleaned_data["guest_name"])
        if not value:
            raise forms.ValidationError("اسم الضيف مطلوب.")
        return value

    def clean_guest_phone(self):
        value = self.cleaned_data["guest_phone"].strip()
        if not value:
            return ""
        try:
            return invoice_phone(value)
        except InvalidPhoneNumber as exc:
            raise forms.ValidationError("أدخل رقمًا دوليًا صحيحًا مع مفتاح الدولة.") from exc


class InvoiceSendForm(forms.Form):
    channel = forms.ChoiceField(choices=[("email", "email"), ("whatsapp", "whatsapp")])
    recipient = forms.CharField(max_length=254)
    confirm_recipient = forms.BooleanField(label="أؤكد أن بيانات المستلم صحيحة وأوافق على الإرسال.")

    def clean(self):
        cleaned = super().clean()
        recipient = cleaned.get("recipient", "").strip()
        try:
            if cleaned.get("channel") == "email":
                cleaned["recipient"] = forms.EmailField().clean(recipient).lower()
            elif cleaned.get("channel") == "whatsapp":
                cleaned["recipient"] = invoice_phone(recipient)
        except (forms.ValidationError, InvalidPhoneNumber):
            self.add_error(
                "recipient", "راجع بيانات المستلم؛ لا يمكن الإرسال بهذا العنوان أو الرقم."
            )
        return cleaned
