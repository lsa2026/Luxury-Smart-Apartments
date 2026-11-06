"""Morning-only HyperPay billing guards, shared by guest and operations forms.

Gateway limits are checked before creating a checkout, including saved admin
addresses. Do not truncate, fabricate, or silently rewrite billing fields.
"""

import re

from django import forms

from apps.payments.payment_link_views import PaymentLinkBillingForm

FIELD_RULES = {
    "billing_street1": (2, 50, "العنوان المرتبط ببطاقة الدفع: من حرفين إلى 50 حرفًا."),
    "billing_city": (2, 45, "اسم المدينة: من حرفين إلى 45 حرفًا."),
    "billing_state": (2, 50, "اسم المنطقة أو المحافظة: من حرفين إلى 50 حرفًا."),
    "billing_postcode": (1, 10, "الرمز البريدي: للسعودية 5 أرقام، وللدول الأخرى حتى 10 أحرف أو أرقام."),
}
POSTCODE_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*$")


class MorningGuestBillingForm(PaymentLinkBillingForm):
    """Guest consent remains on this form; billing rules apply only to MorningStay."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, (minimum, maximum, rule) in FIELD_RULES.items():
            field = self.fields[name]
            field.min_length = minimum
            field.max_length = maximum
            field.help_text = rule
            field.error_messages.update({
                "required": "هذا الحقل مطلوب لإتمام الدفع. " + rule,
                "min_length": "القيمة أقصر من المطلوب. " + rule,
                "max_length": "القيمة أطول من المسموح في HyperPay. " + rule,
                "invalid": "تنسيق الحقل غير صحيح. " + rule,
            })
            field.widget.attrs.update({
                "minlength": str(minimum),
                "maxlength": str(maximum),
                "aria-describedby": f"id_{name}_rule",
                "data-morning-billing": name,
                "title": rule,
            })
        self.fields["billing_postcode"].widget.attrs["placeholder"] = "12345"
        self.fields["billing_country"].help_text = (
            "اختر دولة عنوان الفوترة الفعلي، وليس بالضرورة دولة الشقة."
        )
        self.fields["billing_country"].widget.attrs["aria-describedby"] = (
            "id_billing_country_rule"
        )

    def clean(self):
        cleaned = super().clean()
        postcode = cleaned.get("billing_postcode")
        country = cleaned.get("billing_country")
        if isinstance(postcode, str) and country:
            if country == "SA" and not re.fullmatch(r"[0-9]{5}", postcode):
                self.add_error(
                    "billing_postcode",
                    "الرمز البريدي السعودي يجب أن يتكون من 5 أرقام فقط، مثل 12345. "
                    "لا تدخل اسم الشارع أو اسم الشخص في هذا الحقل.",
                )
            elif country != "SA" and not POSTCODE_PATTERN.fullmatch(postcode):
                self.add_error(
                    "billing_postcode",
                    "أدخل رمزًا بريديًا صحيحًا للدولة المختارة (حد أقصى 10 خانات؛ "
                    "أحرف لاتينية وأرقام ومسافة أو شرطة).",
                )
        return cleaned


class MorningBillingForm(MorningGuestBillingForm):
    """Operations can prepare the address but cannot accept guest legal terms."""

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
        return {"current_language_code": "ar", "morning_billing_rules": True}
    return {
        "current_language_code": "ar",
        "morning_billing_rules": True,
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
