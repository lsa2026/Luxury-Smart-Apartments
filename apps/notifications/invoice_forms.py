"""Bounded owner input; imported values are never trusted as HTML or headers."""

import re

import phonenumbers
from django import forms

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.notifications.invoice_phone import normalize_invoice_phone


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
        required=False,
        min_value=1,
        max_value=9223372036854775807,
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
    )
    guest_phone_country = forms.ChoiceField(
        label="بلد الرقم المحلي",
        required=False,
        initial="SA",
        choices=[
            (code, f"{code} (+{phonenumbers.country_code_for_region(code)})")
            for code in ["SA", *sorted(phonenumbers.SUPPORTED_REGIONS - {"SA"})]
        ],
        widget=forms.Select(attrs={"data-invoice-phone-country": ""}),
    )
    guest_phone = forms.CharField(
        label="رقم جوال الضيف",
        max_length=96,
        required=False,
        widget=forms.TextInput(
            attrs={
                "inputmode": "tel",
                "autocomplete": "off",
                "dir": "ltr",
                "placeholder": "0501234567",
                "aria-describedby": "invoice-phone-help",
            }
        ),
    )

    def clean(self):
        cleaned = super().clean()
        phone = cleaned.get("guest_phone", "").strip()
        if cleaned.get("hostaway_id") and phone:
            raise forms.ValidationError("ابحث برقم الحجز أو رقم الجوال، وليس الاثنين معًا.")
        if not cleaned.get("hostaway_id") and not phone and not self.errors:
            raise forms.ValidationError("أدخل رقم الحجز أو رقم جوال الضيف.")
        if phone and "guest_phone_country" not in self.errors:
            try:
                cleaned["guest_phone"] = normalize_invoice_phone(
                    phone, country=cleaned.get("guest_phone_country") or "SA"
                )
            except InvalidPhoneNumber:
                self.add_error("guest_phone", "راجع رقم الجوال ومفتاح الدولة المختار.")
        return cleaned


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
