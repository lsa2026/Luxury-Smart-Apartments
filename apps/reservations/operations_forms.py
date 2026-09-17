"""Owner-only forms for a manual booking draft."""

from __future__ import annotations

from decimal import Decimal

from django import forms
from django.utils import timezone
from django.utils.html import strip_tags

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.properties.models import Property

from .models import ManualBookingDraft


def _clean_text(value: str) -> str:
    without_markup = strip_tags(value)
    return "".join(character for character in without_markup if character.isprintable()).strip()


class ManualBookingAvailabilityForm(forms.Form):
    property = forms.ModelChoiceField(
        label="الوحدة",
        queryset=Property.objects.none(),
        empty_label="اختر الوحدة",
    )
    check_in = forms.DateField(
        label="تاريخ الوصول",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    check_out = forms.DateField(
        label="تاريخ المغادرة",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    guests = forms.IntegerField(label="عدد الضيوف", min_value=1, initial=1)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.fields["property"].queryset = Property.objects.filter(
            is_visible=True,
            hostaway_is_active=True,
        ).order_by("city_ar", "name_ar", "name_en")

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        property_obj = cleaned.get("property")
        check_in = cleaned.get("check_in")
        check_out = cleaned.get("check_out")
        guests = cleaned.get("guests")
        if check_in and check_in < timezone.localdate():
            self.add_error("check_in", "لا يمكن أن يكون الوصول في تاريخ مضى.")
        if check_in and check_out and check_out <= check_in:
            self.add_error("check_out", "يجب أن يكون تاريخ المغادرة بعد تاريخ الوصول.")
        if (
            property_obj
            and guests
            and property_obj.person_capacity
            and guests > property_obj.person_capacity
        ):
            self.add_error("guests", "عدد الضيوف يتجاوز السعة المعتمدة لهذه الوحدة.")
        return cleaned


class ManualBookingFinalizeForm(forms.Form):
    guest_first_name = forms.CharField(label="الاسم الأول", max_length=100)
    guest_last_name = forms.CharField(label="اسم العائلة", max_length=100)
    guest_email = forms.EmailField(label="البريد الإلكتروني", max_length=254)
    guest_phone = forms.CharField(
        label="رقم الجوال",
        max_length=32,
        widget=forms.TextInput(attrs={"dir": "ltr", "inputmode": "tel", "placeholder": "+966…"}),
    )
    final_total_price = forms.DecimalField(
        label="السعر النهائي",
        max_digits=14,
        decimal_places=4,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    price_override_reason = forms.CharField(
        label="سبب تعديل السعر",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    special_requests = forms.CharField(
        label="ملاحظات تشغيلية",
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def __init__(
        self,
        *args: object,
        draft: ManualBookingDraft,
        **kwargs: object,
    ) -> None:
        self.draft = draft
        super().__init__(*args, **kwargs)
        self.fields["final_total_price"].help_text = f"بالعملة الأصلية للحجز: {draft.currency}."
        if not self.is_bound:
            self.initial.update(
                {
                    "final_total_price": draft.final_total_price,
                    "guest_first_name": draft.guest_first_name,
                    "guest_last_name": draft.guest_last_name,
                    "guest_email": draft.guest_email,
                    "guest_phone": draft.guest_phone,
                    "price_override_reason": draft.price_override_reason,
                    "special_requests": draft.special_requests,
                }
            )

    def clean_guest_first_name(self) -> str:
        value = _clean_text(self.cleaned_data["guest_first_name"])
        if not value:
            raise forms.ValidationError("اسم الضيف مطلوب.")
        return value

    def clean_guest_last_name(self) -> str:
        value = _clean_text(self.cleaned_data["guest_last_name"])
        if not value:
            raise forms.ValidationError("اسم العائلة مطلوب.")
        return value

    def clean_guest_email(self) -> str:
        return self.cleaned_data["guest_email"].strip().casefold()

    def clean_guest_phone(self) -> str:
        try:
            return normalize_phone_number(_clean_text(self.cleaned_data["guest_phone"]))
        except InvalidPhoneNumber as exc:
            raise forms.ValidationError(
                "أدخل رقمًا دوليًا صالحًا يبدأ بعلامة +، مثل +966500000000."
            ) from exc

    def clean_price_override_reason(self) -> str:
        return _clean_text(self.cleaned_data["price_override_reason"])

    def clean_special_requests(self) -> str:
        return _clean_text(self.cleaned_data["special_requests"])

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        final_price = cleaned.get("final_total_price")
        reason = cleaned.get("price_override_reason")
        if final_price is not None and final_price != self.draft.system_total_price:
            if not isinstance(reason, str) or len(reason) < 10:
                self.add_error(
                    "price_override_reason",
                    "عند تعديل سعر Hostaway اكتب سببًا واضحًا لا يقل عن 10 أحرف.",
                )
        return cleaned


class ManualBookingCancelForm(forms.Form):
    """Require an explicit acknowledgement before cancelling a local draft."""

    confirm_cancellation = forms.BooleanField(
        label="أؤكد إلغاء هذه المسودة فقط",
        error_messages={"required": "أكد إلغاء المسودة قبل المتابعة."},
    )
