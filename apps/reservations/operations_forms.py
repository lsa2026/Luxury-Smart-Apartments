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
        self.fields["property"].widget.attrs.update(
            {
                "class": "lsa-manual-booking__property-select",
                "data-calendar-property-select": "",
            }
        )
        for field_name in ("check_in", "check_out"):
            self.fields[field_name].widget.attrs.update(
                {
                    "class": "lsa-manual-booking__date-input",
                    "min": timezone.localdate().isoformat(),
                }
            )

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


class ManualBookingDeleteForm(forms.Form):
    """Require acknowledgement before permanently deleting an uncharged draft."""

    confirm_deletion = forms.BooleanField(
        label="أفهم أن المسودة ستُحذف نهائيًا من قائمة المسودات",
        error_messages={"required": "أكد الحذف النهائي قبل المتابعة."},
    )


class CancellationDecisionForm(forms.Form):
    """A deliberate local decision; it never sends a Hostaway request."""

    decision_note = forms.CharField(
        label="ملاحظة القرار",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def clean_decision_note(self) -> str:
        return _clean_text(self.cleaned_data["decision_note"])


class CancellationRejectionForm(CancellationDecisionForm):
    """A rejection must remain explainable to the guest and future operators."""

    def clean_decision_note(self) -> str:
        value = super().clean_decision_note()
        if len(value) < 10:
            raise forms.ValidationError("اكتب سبب الرفض بوضوح (10 أحرف على الأقل).")
        return value


class CancellationExecutionForm(forms.Form):
    """One explicit owner confirmation before a live cancellation is sent."""

    confirm_external_cancellation = forms.BooleanField(
        label="أؤكد إلغاء الحجز وإرسال الاسترداد إلى وسيلة الدفع الأصلية",
        error_messages={"required": "أكد الإلغاء الخارجي قبل المتابعة."},
    )

    approved_refund_amount = forms.DecimalField(
        label="مبلغ الاسترداد المعتمد",
        max_digits=14,
        decimal_places=4,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    refund_decision_note = forms.CharField(
        label="ملاحظة القرار",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args: object, maximum_amount: Decimal, **kwargs: object) -> None:
        self.maximum_amount = maximum_amount
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["approved_refund_amount"] = maximum_amount

    def clean_refund_decision_note(self) -> str:
        return _clean_text(self.cleaned_data["refund_decision_note"])

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        amount = cleaned.get("approved_refund_amount")
        note = cleaned.get("refund_decision_note")
        if amount is not None and amount > self.maximum_amount:
            self.add_error(
                "approved_refund_amount",
                "لا يمكن أن يتجاوز الاسترداد مبلغ الحجز المدفوع.",
            )
        if amount is not None and amount != self.maximum_amount and (
            not isinstance(note, str) or len(note) < 10
        ):
            self.add_error(
                "refund_decision_note",
                "اشرح سبب الاسترداد الجزئي أو الصفري بوضوح (10 أحرف على الأقل).",
            )
        return cleaned


class OwnerModificationExecutionForm(CancellationExecutionForm):
    """Owner confirmation for an external date change and any price decrease."""

    confirm_external_cancellation = None
    confirm_external_modification = forms.BooleanField(
        label="أؤكد تنفيذ التعديل في Hostaway ثم تنفيذ الاسترداد المعتمد إن وُجد",
        error_messages={"required": "أكد تنفيذ التعديل الخارجي قبل المتابعة."},
    )


class RefundDecisionForm(forms.Form):
    """Records an owner-approved full or partial refund without moving money."""

    approved_amount = forms.DecimalField(
        label="مبلغ الاسترداد المعتمد",
        max_digits=14,
        decimal_places=4,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    decision_note = forms.CharField(
        label="سبب القرار",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args: object, current_amount: Decimal, **kwargs: object) -> None:
        self.current_amount = current_amount
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["approved_amount"] = current_amount

    def clean_decision_note(self) -> str:
        return _clean_text(self.cleaned_data["decision_note"])

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        amount = cleaned.get("approved_amount")
        note = cleaned.get("decision_note")
        if amount is not None and amount != self.current_amount and (
            not isinstance(note, str) or len(note) < 10
        ):
            self.add_error(
                "decision_note",
                "اشرح بوضوح سبب تغيير مبلغ الاسترداد (10 أحرف على الأقل).",
            )
        return cleaned


class RefundSettlementForm(forms.Form):
    """Records a completed external transfer; it does not call a payment provider."""

    transfer_reference = forms.CharField(label="مرجع التحويل", max_length=100)
    settlement_note = forms.CharField(
        label="ملاحظة التحويل",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    confirm_settlement = forms.BooleanField(
        label="أؤكد أن المبلغ حُوِّل فعليًا إلى الضيف",
        error_messages={"required": "أكد تنفيذ التحويل الفعلي قبل تسجيله."},
    )

    def clean_transfer_reference(self) -> str:
        value = _clean_text(self.cleaned_data["transfer_reference"])
        if len(value) < 2:
            raise forms.ValidationError("أدخل مرجع التحويل أو الرقم البنكي الصحيح.")
        return value

    def clean_settlement_note(self) -> str:
        return _clean_text(self.cleaned_data["settlement_note"])


class RefundGatewaySubmitForm(forms.Form):
    """A separate confirmation makes a live refund an intentional owner action."""

    confirm_gateway_refund = forms.BooleanField(
        label="أؤكد إعادة هذا المبلغ إلى بطاقة الضيف عبر HyperPay",
        error_messages={"required": "أكد تنفيذ الاسترداد عبر بوابة الدفع قبل المتابعة."},
    )
