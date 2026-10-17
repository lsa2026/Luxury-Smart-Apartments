"""Additional guest credential; existing authentication routes remain unchanged."""

import unicodedata

import phonenumbers
from django import forms
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.reservations.security import mask_phone, session_key_hash

from .models import CustomerProfile, PhoneChallenge, VerifiedPhone
from .phone_auth import PENDING, _limit, allowed, begin, consume, ready, recent_primary
from .phone_copy import phone_copy


class PhoneForm(forms.Form):
    phone_country = forms.ChoiceField(
        required=False,
        initial="SA",
        choices=[
            (code, f"{code} (+{phonenumbers.country_code_for_region(code)})")
            for code in ["SA", *sorted(phonenumbers.SUPPORTED_REGIONS - {"SA"})]
        ],
        widget=forms.Select(attrs={"data-phone-country": ""}),
    )
    phone = forms.CharField(
        max_length=32,
        widget=forms.TextInput(
            attrs={
                "type": "tel",
                "autocomplete": "tel",
                "inputmode": "tel",
                "dir": "ltr",
                "placeholder": "0501234567",
                "data-guest-phone": "",
                "aria-describedby": "account-phone-help account-phone-validation",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["phone"].label = phone_copy()["phone"]
        self.fields["phone_country"].label = phone_copy()["country"]

    def clean_phone(self):
        try:
            return normalize_phone_number(
                self.cleaned_data["phone"],
                default_region=self.cleaned_data.get("phone_country") or None,
            )
        except InvalidPhoneNumber as exc:
            raise forms.ValidationError(phone_copy()["invalid_phone"]) from exc


@never_cache
@require_http_methods(["POST"])
def phone_format(request):
    """Formatting only: never look up an account, bind a phone or send a code."""
    if not ready():
        raise Http404
    return _phone_format_response(request)


@login_required
@never_cache
@require_http_methods(["POST"])
def profile_phone_format(request):
    """Contact editing stays independent of the WhatsApp sign-in pilot."""
    return _phone_format_response(request)


def _phone_format_response(request):
    if not _limit("format-global", "site", 300, 300) or not _limit(
        "format-ip", request.META.get("REMOTE_ADDR", ""), 60, 300
    ):
        return JsonResponse({"detail": "temporarily_unavailable"}, status=429)
    form = PhoneForm(
        {"phone": request.POST.get("phone", ""), "phone_country": request.POST.get("country", "SA")}
    )
    if not form.is_valid():
        return JsonResponse({"detail": "invalid_phone"}, status=400)
    phone = form.cleaned_data["phone"]
    region = phonenumbers.region_code_for_number(phonenumbers.parse(phone, None))
    return JsonResponse(
        {
            "phone": phone,
            "country": region
            if region in phonenumbers.SUPPORTED_REGIONS
            else form.cleaned_data["phone_country"] or "SA",
        }
    )


def _next(request, value):
    return (
        value
        if url_has_allowed_host_and_scheme(
            value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
        else ""
    )


@never_cache
@require_http_methods(["GET", "POST"])
def phone_start(request, *, binding=False):
    if not ready():
        raise Http404
    labels = phone_copy()
    if binding and not request.user.is_authenticated:
        return redirect("accounts:login")
    if not binding and request.user.is_authenticated:
        return redirect("accounts:dashboard")
    error = ""
    can_send = True
    if binding:
        if not allowed(request.user):
            raise Http404
        if not recent_primary(request):
            error, can_send = labels["recent"], False
        elif VerifiedPhone.objects.filter(user=request.user).exists():
            error, can_send = labels["existing"], False
    initial = {}
    if not binding and request.method == "GET":
        contact = request.session.pop("guest_checkout_phone", "")
        if contact:
            initial["phone"] = contact
            try:
                region = phonenumbers.region_code_for_number(phonenumbers.parse(contact, None))
                if region in phonenumbers.SUPPORTED_REGIONS:
                    initial["phone_country"] = region
            except phonenumbers.NumberParseException:
                pass
    if binding and request.method == "GET":
        contact = (
            CustomerProfile.objects.filter(user=request.user)
            .values_list("phone", flat=True)
            .first()
        )
        if contact:
            initial["phone"] = contact
            try:
                region = phonenumbers.region_code_for_number(phonenumbers.parse(contact, None))
                if region in phonenumbers.SUPPORTED_REGIONS:
                    initial["phone_country"] = region
            except phonenumbers.NumberParseException:
                pass
    form = PhoneForm(request.POST if request.method == "POST" else None, initial=initial)
    next_url = _next(
        request,
        request.POST.get("next", "") if request.method == "POST" else request.GET.get("next", ""),
    )
    if request.method == "POST" and can_send and form.is_valid():
        challenge = begin(
            request, form.cleaned_data["phone"], purpose="bind" if binding else "login"
        )
        if challenge:
            request.session["guest_phone_next"] = next_url
            return redirect("accounts:phone_confirm")
        error = labels["bind_error"] if binding else labels["invalid"]
    return render(
        request,
        "accounts/phone.html",
        {
            "phone_copy": labels,
            "phone_form": form,
            "phone_binding": binding,
            "phone_can_send": can_send,
            "phone_error": error,
            "next": next_url,
        },
    )


@never_cache
@require_http_methods(["GET", "POST"])
def phone_confirm(request):
    if not ready():
        raise Http404
    challenge = PhoneChallenge.objects.filter(
        pk=request.session.get(PENDING), session_digest=session_key_hash(request)
    ).first()
    if not challenge:
        if request.GET.get("delivery") == "1":
            raise Http404
        return redirect("accounts:phone_login")
    binding = challenge.purpose == "bind"
    if binding and (
        not request.user.is_authenticated
        or request.user.pk != challenge.user_id
        or not allowed(request.user)
    ):
        raise Http404
    delivery = ""
    if binding:
        if challenge.expires_at <= timezone.now():
            delivery = "expired"
        elif challenge.status in {"pending", "sending"}:
            delivery = "waiting"
        elif challenge.status == "sent":
            delivery = "accepted"
        else:
            delivery = "failed"
    if request.GET.get("delivery") == "1":
        # Authenticated binding only. Anonymous sign-in must not disclose whether
        # a number/account exists or whether it received a message.
        if not binding or request.method != "GET":
            raise Http404
        return JsonResponse({"state": delivery})
    labels, error = phone_copy(), ""
    if request.method == "POST":
        raw = request.POST.get("code", "").strip()
        code = "".join(str(unicodedata.decimal(v)) if v.isdecimal() else v for v in raw)
        row = consume(request, code)
        if row:
            next_url = _next(request, request.session.pop("guest_phone_next", ""))
            if row.purpose == "bind":
                messages.success(request, labels["success"])
                return redirect("accounts:details")
            request._guest_phone_authenticated = True
            login(request, row.user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect(next_url or "accounts:dashboard")
        error = labels["invalid"]
    return render(
        request,
        "accounts/phone.html",
        {
            "phone_copy": labels,
            "phone_confirmation": True,
            "phone_binding": binding,
            "phone_delivery": delivery,
            "phone_error": error,
            "phone_masked": mask_phone(challenge.phone),
            "next": _next(request, request.session.get("guest_phone_next", "")),
        },
    )
