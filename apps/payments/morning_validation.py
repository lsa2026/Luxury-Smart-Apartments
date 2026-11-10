"""Shared approved billing contract; see docs/morning-hyperpay-validation.md."""

import re

from django import forms
from django.core.exceptions import ValidationError
from django.utils import translation

from apps.core.guest_documents import documents_digest
from apps.payments.countries import ISO_ALPHA2_COUNTRY_CODES


def language():
    value = translation.get_language()
    return value if value in {"en", "fr"} else "ar"


def copy(ar, en, fr=None):
    current = language()
    return (fr or en) if current == "fr" else en if current == "en" else ar


# HTML's email syntax, with the OPPWA documented total length. No DNS checks.
EMAIL_PATTERN = (
    r"(?=[\s\S]{6,128}$)[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*"
)

# These patterns are used verbatim by Python and the browser, via widget attributes.
# Required state/email are existing merchant requirements, not universal API requirements.
RULES = {
    "guest_first_name": (
        "[\\s\\S]{1,50}",
        "الاسم الأول: من ١ إلى ٥٠ حرفًا، وتُقبل العربية.",
        "First name: 1–50 characters; Arabic is accepted.",
        "Prénom : 1 à 50 caractères ; l’arabe est accepté.",
    ),
    "guest_last_name": (
        "[\\s\\S]{1,50}",
        "اسم العائلة: من ١ إلى ٥٠ حرفًا، وتُقبل العربية.",
        "Surname: 1–50 characters; Arabic is accepted.",
        "Nom : 1 à 50 caractères ; l’arabe est accepté.",
    ),
    "guest_email": (
        EMAIL_PATTERN,
        "بريد إلكتروني صحيح من ٦ إلى ١٢٨ حرفًا.",
        "A valid email address of 6–128 characters.",
        "Une adresse e-mail valide de 6 à 128 caractères.",
    ),
    "billing_street1": (
        "(?![0-9]+$)[\\s\\S]{1,100}",
        "عنوان الفوترة: من ١ إلى ١٠٠ حرف، ولا يتكون من أرقام فقط.",
        "Billing street: 1–100 characters, not only digits.",
        "Rue de facturation : 1 à 100 caractères, pas uniquement des chiffres.",
    ),
    "billing_city": (
        "[\\s\\S]{1,80}",
        "مدينة الفوترة: من ١ إلى ٨٠ حرفًا، وتُقبل العربية.",
        "Billing city: 1–80 characters; Arabic is accepted.",
        "Ville de facturation : 1 à 80 caractères ; l’arabe est accepté.",
    ),
    "billing_state": (
        "[a-zA-Z0-9.]{1,50}",
        "المنطقة مطلوبة لهذا الدفع: من ١ إلى ٥٠ حرفًا إنجليزيًا أو رقمًا أو نقطة، "
        "دون مسافات. مثال: Riyadh.",
        "Region is required for this checkout: 1–50 English letters, digits or dots, "
        "without spaces. Example: Riyadh.",
        "Région obligatoire : 1 à 50 lettres anglaises, chiffres ou points, "
        "sans espaces. Exemple : Riyadh.",
    ),
    "billing_postcode": (
        "[A-Za-z0-9]{1,16}",
        "الرمز البريدي: من ١ إلى ١٦ حرفًا إنجليزيًا أو رقمًا، دون مسافات أو شرطات.",
        "Postal code: 1–16 English letters or digits, without spaces or hyphens.",
        "Code postal : 1 à 16 lettres anglaises ou chiffres, sans espaces ni tirets.",
    ),
    "billing_country": (
        "[A-Z]{2}",
        "اختر بلد عنوان الفوترة من القائمة (رمز ISO من حرفين).",
        "Select the billing country from the list (two-letter ISO code).",
        "Choisissez le pays de facturation dans la liste (code ISO de deux lettres).",
    ),
}
LABELS = {
    "guest_first_name": ("الاسم الأول", "First name", "Prénom"),
    "guest_last_name": ("اسم العائلة", "Surname", "Nom"),
    "guest_email": ("البريد الإلكتروني", "Email address", "Adresse e-mail"),
    "billing_street1": ("عنوان الشارع", "Street address", "Adresse"),
    "billing_city": ("المدينة", "City", "Ville"),
    "billing_state": ("المنطقة أو المحافظة", "State or region", "État ou région"),
    "billing_postcode": ("الرمز البريدي", "Postal code", "Code postal"),
    "billing_country": ("الدولة", "Country", "Pays"),
}
AUTOCOMPLETE = dict(
    zip(
        RULES,
        (
            "given-name",
            "family-name",
            "email",
            "address-line1",
            "address-level2",
            "address-level1",
            "postal-code",
            "country",
        ),
        strict=True,
    )
)


def rule_error(name):
    return copy(*RULES[name][1:])


def validate_value(name, value):
    value = str(value or "").strip(" \t\r\n\f\v")
    if "\x00" in value or not re.fullmatch(RULES[name][0], value):
        raise ValidationError(rule_error(name))
    if name == "billing_country" and value not in ISO_ALPHA2_COUNTRY_CODES:
        raise ValidationError(rule_error(name))
    return value


def customer_values(stay):
    first, _, last = stay.guest_name.strip().partition(" ")
    return {
        "guest_first_name": first,
        "guest_last_name": last.strip(),
        "guest_email": stay.guest_email,
    }


class MorningBillingFields(forms.Form):
    """Never inherit ordinary checkout sanitization or its unrelated field limits."""

    def __init__(self, *args, default_country_code="SA", customer=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._billing_rule_names = set()
        for name, (pattern, ar, en, fr) in RULES.items():
            if name.startswith("guest_") and not customer:
                continue
            hint = copy(ar, en, fr)
            attrs = {
                "autocomplete": AUTOCOMPLETE[name],
                "data-morning-pattern": pattern,
                "data-morning-error": hint,
                "aria-describedby": f"id_{name}_help id_{name}_error",
            }
            if name == "guest_email":
                attrs["inputmode"] = "email"
            field = forms.CharField(strip=False, widget=forms.TextInput(attrs=attrs))
            if name == "billing_country":
                attrs["data-country-select"] = ""
                field = forms.ChoiceField(
                    choices=[
                        ("", copy("اختر البلد", "Choose country", "Choisissez le pays")),
                        *((code, code) for code in sorted(ISO_ALPHA2_COUNTRY_CODES)),
                    ],
                    widget=forms.Select(attrs=attrs),
                )
            field.label = copy(*LABELS[name])
            field.help_text = hint
            field.error_messages.update(required=hint, invalid_choice=hint)
            field.error_messages["null_characters_not_allowed"] = hint
            self.fields[name] = field
            self._billing_rule_names.add(name)
        if not self.is_bound:
            self.initial.setdefault("billing_country", default_country_code)

    def clean(self):
        cleaned = super().clean()
        for name in self._billing_rule_names & cleaned.keys():
            try:
                cleaned[name] = validate_value(name, cleaned[name])
            except ValidationError as exc:
                self.add_error(name, exc)
        return cleaned


class MorningPaymentForm(MorningBillingFields):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, customer=True, **kwargs)
        for name in ("terms_accepted", "house_rules_accepted"):
            message = copy(
                "يرجى قراءة هذا الشرط والموافقة عليه للمتابعة.",
                "Please read and accept this condition to continue.",
                "Veuillez lire et accepter cette condition pour continuer.",
            )
            self.fields[name] = forms.BooleanField(
                error_messages={"required": message},
                help_text=message,
                widget=forms.CheckboxInput(
                    attrs={
                        "data-morning-consent": "",
                        "data-morning-error": message,
                        "aria-describedby": f"id_{name}_error",
                    }
                ),
            )
        self.fields["documents_digest"] = forms.CharField(
            widget=forms.HiddenInput(), initial=documents_digest()
        )

    def clean_documents_digest(self):
        value = self.cleaned_data["documents_digest"]
        if value != documents_digest():
            raise ValidationError(
                copy(
                    "تحدثت الشروط. راجعها ثم وافق مجددًا.",
                    "Conditions changed. Review and accept them again.",
                    "Les conditions ont changé. Relisez-les et acceptez-les à nouveau.",
                )
            )
        return value
