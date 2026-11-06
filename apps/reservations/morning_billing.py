"""Reuse checkout address validation; management never accepts guest terms."""

import json
import re

from django import forms

from apps.payments.payment_link_views import PaymentLinkBillingForm

from .morning_validation import SA_REGIONS, decorate_field, region_code


class MorningPaymentForm(PaymentLinkBillingForm):
    """HyperPay 3DS limits plus the observed MPGS postcode limit, morning only."""

    morning_hints = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        rules = {
            "billing_street1": (
                50,
                1,
                "عنوان الشارع",
                "عنوان بطاقة الدفع الحقيقي؛ من 1 إلى 50 حرفًا، وليس عنوان الشقة بالضرورة.",
            ),
            "billing_city": (45, 2, "المدينة", "اسم المدينة؛ من حرفين إلى 45 حرفًا."),
            "billing_state": (
                50,
                1,
                "المنطقة أو رمزها",
                "السعودية: الرياض أو 01. الدول الأخرى: رمز المنطقة من 1 إلى 3 خانات مثل NY.",
            ),
            "billing_postcode": (
                10,
                1,
                "الرمز البريدي",
                "السعودية: 5 أرقام. دوليًا: حتى 10 خانات لاتينية/أرقام ومسافة أو شرطة.",
            ),
        }
        for name, (maximum, minimum, label, hint) in rules.items():
            self.fields[name] = forms.CharField(
                label=label,
                max_length=maximum,
                min_length=minimum,
                help_text=hint,
                widget=self.fields[name].widget,
                error_messages={
                    "required": f"أدخل {label} المرتبط ببطاقة الدفع.",
                    "max_length": f"{label}: الحد الأقصى {maximum} حرفًا؛ أدخل قيمة أقصر صحيحة.",
                    "min_length": f"{label}: أدخل {minimum} حرفًا على الأقل.",
                },
            )
            self.fields[name].widget.attrs.update(maxlength=maximum, minlength=minimum)
        country = self.fields["billing_country"]
        country.label = "الدولة"
        country.help_text = "اختر دولة عنوان بطاقة الدفع من القائمة؛ يُرسل رمزها الدولي تلقائيًا."
        country.error_messages.update(
            required="اختر دولة عنوان بطاقة الدفع.", invalid_choice="اختر دولة صحيحة من القائمة."
        )
        self.fields["billing_state"].widget.attrs["data-sa-regions"] = json.dumps(
            SA_REGIONS, ensure_ascii=False
        )
        for name, field in self.fields.items():
            if name.startswith("billing_"):
                decorate_field(field, name)
        self.fields["terms_accepted"].error_messages["required"] = (
            "راجع شروط الإقامة ثم وافق عليها للمتابعة."
        )
        self.fields["house_rules_accepted"].error_messages["required"] = (
            "راجع تعليمات المنزل ثم وافق عليها للمتابعة."
        )

    def clean(self):
        data = super().clean()
        for name in ("billing_street1", "billing_city", "billing_state"):
            original = str(self.data.get(name, "")).strip()
            if original and (not original.isprintable() or "<" in original or ">" in original):
                self.add_error(name, "أدخل نص العنوان دون وسوم أو رموز تحكم.")
            elif name in data and not data[name]:
                self.add_error(name, "هذا الجزء من عنوان الفوترة مطلوب؛ أدخل قيمة صحيحة.")
        country, state, postal = (
            data.get(name) for name in ("billing_country", "billing_state", "billing_postcode")
        )
        if country and state:
            try:
                region_code(state, country)
            except forms.ValidationError as exc:
                self.add_error("billing_state", exc)
        if postal:
            if country == "SA" and not re.fullmatch(r"[0-9]{5}", postal):
                self.add_error(
                    "billing_postcode",
                    "الرمز البريدي السعودي: 5 أرقام فقط مثل 13221؛ لا تدخل اسم الشارع.",
                )
            elif not re.fullmatch(r"[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*", postal):
                self.add_error(
                    "billing_postcode",
                    "الرمز البريدي: حتى 10 خانات لاتينية أو أرقام، مع مسافة أو شرطة فقط.",
                )
        return data


class MorningBillingForm(MorningPaymentForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in list(self.fields):
            if not name.startswith("billing_"):
                self.fields.pop(name)


def saved_billing(stay):
    form = MorningBillingForm(stay.billing_address)
    return dict(form.cleaned_data) if form.is_valid() else None


def address_scope(request):
    from types import SimpleNamespace

    from .security import session_key_hash

    return SimpleNamespace(
        pk=f"morning-admin:{request.user.pk}", session_key_hash=session_key_hash(request)
    )


def address_context(request):
    from django.urls import reverse

    from .address_places import make_address_ticket, places_enabled

    if not places_enabled():
        return {"current_language_code": "ar"}
    return {
        "current_language_code": "ar",
        "address_places_enabled": True,
        "address_places_ticket": make_address_ticket(address_scope(request)),
        "address_suggestions_url": reverse("notifications:morning_address", args=["suggestions"]),
        "address_details_url": reverse("notifications:morning_address", args=["details"]),
    }


def address_view(request, kind):
    from .address_places import CheckoutAddressView

    class AdminAddressView(CheckoutAddressView):
        def get_address_scope(self, request, reference):
            return address_scope(request)

    return AdminAddressView.as_view(details=kind == "details")(request, reference="morning-admin")
