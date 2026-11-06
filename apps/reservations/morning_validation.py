"""Morning-only input contract; no provider requests or silent truncation."""

import re
import unicodedata

from django import forms
from django.core.exceptions import ValidationError

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number


class MorningInputError(ValidationError):
    """Field errors safe to display; no provider details or guest values."""


# ISO 3166-2:SA (SASO codes). Only exact known names are converted.
SA_REGIONS = {
    "01": ("الرياض", "رياض", "Riyadh", "Ar Riyad", "Ar Riyāḑ"),
    "02": ("مكة", "مكة المكرمة", "Makkah", "Mecca"),
    "03": ("المدينة", "المدينة المنورة", "Al Madinah", "Medina"),
    "04": ("الشرقية", "المنطقة الشرقية", "Eastern", "Eastern Province", "Ash Sharqiyah"),
    "05": ("القصيم", "Al Qassim", "Al Qasim", "Qassim"),
    "06": ("حائل", "Hail", "Ha'il"),
    "07": ("تبوك", "Tabuk"),
    "08": ("الحدود الشمالية", "Northern Borders", "Al Hudud ash Shamaliyah"),
    "09": ("جازان", "جيزان", "Jazan", "Jizan"),
    "10": ("نجران", "Najran"),
    "11": ("الباحة", "Al Bahah", "Al Baha"),
    "12": ("الجوف", "Al Jawf"),
    "14": ("عسير", "Asir", "'Asir"),
}


def _region_key(value):
    text = "".join(
        char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char)
    ).casefold()
    return re.sub(r"[\s.'’-]+", "", text.removesuffix(" Province").removesuffix(" province"))


def region_code(value, country):
    value = value.strip()
    if value.upper().startswith(f"{country}-"):
        value = value[3:]
    if country == "SA":
        if value in SA_REGIONS:
            return value
        for code, names in SA_REGIONS.items():
            if _region_key(value) in {_region_key(name) for name in names}:
                return code
        raise forms.ValidationError("اختر منطقة سعودية صحيحة، مثل الرياض أو الرمز 01.")
    if not re.fullmatch(r"[A-Za-z0-9]{1,3}", value):
        raise forms.ValidationError(
            "أدخل رمز المنطقة من 1 إلى 3 أحرف لاتينية أو أرقام، مثل NY؛ لا تكتب اسمًا طويلًا."
        )
    return value.upper()


def clean_name(value):
    value = " ".join(value.split())
    first, _, last = value.partition(" ")
    if not first or not last or not all(any(c.isalpha() for c in part) for part in (first, last)):
        raise forms.ValidationError("أدخل الاسم الأول واسم العائلة؛ لا يُقبل اسم واحد أو أرقام فقط.")
    if len(first) > 50 or len(last) > 50:
        raise forms.ValidationError("الاسم الأول واسم العائلة: 50 حرفًا كحد أقصى لكل جزء.")
    if not value.isprintable() or "<" in value or ">" in value:
        raise forms.ValidationError("أدخل الاسم الحقيقي دون وسوم أو رموز تحكم.")
    return value


def clean_phone(value):
    value = value.strip()
    if not value.startswith("+"):
        raise forms.ValidationError("ابدأ رقم الجوال بـ + ثم رمز الدولة، مثل +966501234567.")
    try:
        return normalize_phone_number(value)
    except InvalidPhoneNumber as exc:
        raise forms.ValidationError("رقم الجوال غير صحيح؛ راجع رمز الدولة وعدد الأرقام.") from exc


class MorningGuestForm(forms.Form):
    guest_name = forms.CharField(
        label="اسم الضيف الكامل",
        max_length=101,
        help_text="الاسم الأول واسم العائلة؛ 50 حرفًا كحد أقصى لكل جزء. العربية مقبولة.",
        error_messages={
            "required": "أدخل الاسم الأول واسم العائلة.",
            "max_length": "الاسم الأول واسم العائلة: 50 حرفًا كحد أقصى لكل جزء.",
        },
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )
    guest_email = forms.EmailField(
        label="البريد الإلكتروني",
        min_length=6,
        max_length=128,
        help_text="بريد صحيح مثل name@example.com؛ من 6 إلى 128 حرفًا.",
        error_messages={
            "required": "أدخل بريد الضيف الإلكتروني.",
            "invalid": "صيغة البريد غير صحيحة؛ استخدم مثل name@example.com.",
            "min_length": "البريد الإلكتروني يجب ألا يقل عن 6 أحرف.",
            "max_length": "البريد الإلكتروني يجب ألا يتجاوز 128 حرفًا.",
        },
        widget=forms.EmailInput(attrs={"autocomplete": "email", "dir": "ltr"}),
    )
    guest_phone = forms.CharField(
        label="رقم الجوال الدولي",
        max_length=32,
        help_text="رقم دولي صحيح يبدأ بـ + ورمز الدولة؛ حتى 15 رقمًا، مثل +966501234567.",
        error_messages={
            "required": "أدخل رقم جوال دوليًا صحيحًا.",
            "max_length": "رقم الجوال أطول من المسموح؛ أدخله بصيغته الدولية.",
        },
        widget=forms.TextInput(attrs={"autocomplete": "tel", "dir": "ltr", "inputmode": "tel"}),
    )

    def clean_guest_name(self):
        return clean_name(self.cleaned_data["guest_name"])

    def clean_guest_phone(self):
        return clean_phone(self.cleaned_data["guest_phone"])


def decorate_field(field, name):
    field.widget.attrs.update(
        {
            "data-morning-rule": name,
            "data-rule-help": field.help_text,
            "aria-describedby": f"id_{name}_rule id_{name}_error",
        }
    )
