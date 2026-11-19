from django import forms
from django.core.exceptions import ValidationError

from .assisted_attribution import lookup


class AttributionInputForm(forms.Form):
    attribution_reference = forms.CharField(
        required=False,
        max_length=36,
        label="مرجع الاستفسار من رسالة واتساب",
        widget=forms.TextInput(attrs={"dir": "ltr", "autocomplete": "off", "placeholder": "LSA-…"}),
        help_text=(
            "انسخ المرجع الموجود في رسالة الضيف لربط مصدر الزيارة. "
            "اتركه فارغًا إن لم يوجد؛ لا تدخل رقم الجوال أو رقم الحجز."
        ),
    )
    attribution_staff_source = forms.ChoiceField(
        required=False,
        label="مصدر ذكره الضيف — إفادة غير متحققة",
        initial="unknown",
        choices=[
            ("unknown", "غير معلوم"),
            ("google_ads", "ذكر إعلان جوجل"),
            ("social", "ذكر وسائل التواصل"),
            ("referral", "توصية أو إحالة"),
            ("returning", "ضيف عائد"),
            ("other", "مصدر آخر"),
        ],
        help_text="تُحفظ إفادة الموظف مستقلة عن المصدر الملتقط، ولا تثبت حملة إعلانية.",
    )

    def clean_attribution_reference(self):
        value = self.cleaned_data["attribution_reference"].strip().upper()
        if value:
            row = lookup(value)
            if hasattr(row, "binding"):
                anchor = getattr(getattr(self, "draft", None), "quote_id", None)
                same = bool(anchor and row.binding.quote_id == anchor)
                for field in ("morning_stay", "day_use_stay"):
                    stay = getattr(row.binding, field)
                    if stay and str(stay.request_id) == self.data.get("request_id"):
                        same = True
                if not same:
                    raise ValidationError("استُخدم هذا المرجع لطلب آخر.")
        return value

    def clean_attribution_staff_source(self):
        return self.cleaned_data["attribution_staff_source"] or "unknown"
