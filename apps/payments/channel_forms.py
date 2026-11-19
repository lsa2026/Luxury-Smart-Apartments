"""Non-card inputs only. Cardholder billing is never copied from the guest."""

import re
from decimal import Decimal

from django import forms
from django.utils import timezone

from .morning_validation import MorningBillingFields


class ChannelCollectionForm(MorningBillingFields):
    hostaway_id = forms.RegexField(r"^[1-9][0-9]{0,18}$", label="رقم حجز هوستاوي الخارجي")
    amount = forms.DecimalField(
        label="رصيد بطاقة بوكينج المتبقي",
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    currency = forms.ChoiceField(choices=[("SAR", "SAR — ريال سعودي")], label="عملة البطاقة")
    available_on = forms.DateField(
        label="متاح للتحصيل من", widget=forms.DateInput(attrs={"type": "date"})
    )
    charge_before = forms.DateField(
        label="يجب التحصيل قبل", widget=forms.DateInput(attrs={"type": "date"})
    )
    balance_confirmed = forms.BooleanField(
        label=(
            "تحققت الآن من رصيد البطاقة وإتاحتها في بوكينج ومن عدم تحصيله سابقًا؛ "
            "قد تتأخر قائمة بوكينج ٣٠ دقيقة."
        )
    )
    payer_confirmed = forms.BooleanField(
        label="بيانات الفوترة أدناه تخص حامل البطاقة حسب المصدر المعتمد، وليست بيانات ضيف افتراضية."
    )

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("default_country_code", "")
        super().__init__(*args, customer=True, **kwargs)
        self.fields["guest_first_name"].label = "اسم حامل البطاقة الأول حسب بيانات الفوترة المعتمدة"
        self.fields["guest_last_name"].label = "اسم حامل البطاقة الأخير حسب بيانات الفوترة المعتمدة"
        self.fields["guest_email"].label = "بريد الفوترة المعتمد لهذه البطاقة"
        for name in self._billing_rule_names:
            self.fields[name].widget.attrs["autocomplete"] = "off"

    def clean(self):
        values = super().clean()
        if self.is_bound and any(
            key.casefold().replace("_", "")
            in {"pan", "cardnumber", "cvv", "cvc", "expiry", "cardexpiry"}
            for key in self.data
        ):
            raise forms.ValidationError(
                "لا تدخل رقم البطاقة أو رمزها في هذه الصفحة؛ بيانات البطاقة تدخل لدى البوابة فقط."
            )
        for name in self._billing_rule_names:
            value = values.get(name, "")
            if re.search(r"(?:[0-9][ -]?){13,19}", value):
                self.add_error(name, "لا تدخل أرقام البطاقات في بيانات الفوترة.")
        start, deadline = values.get("available_on"), values.get("charge_before")
        if deadline and deadline <= timezone.localdate():
            self.add_error(
                "charge_before", "انتهت مهلة التحصيل أو تبدأ اليوم؛ راجع الموعد في بوكينج."
            )
        if start and deadline and start >= deadline:
            self.add_error("available_on", "بداية الإتاحة يجب أن تسبق مهلة التحصيل.")
        return values
