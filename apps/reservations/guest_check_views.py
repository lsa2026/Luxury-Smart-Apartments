"""Private reporting check only: never save a booking/contact or send a message."""

import uuid
from dataclasses import asdict

from django import forms
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import translation
from django.views.decorators.http import require_POST

from apps.accounts.access import operations_access_required
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_booking_phone

from .booking_forms import GuestDetailsForm
from .models import Reservation
from .services.guest_classification import classify_prospective, classify_reference
from .services.guest_classification_history import ensure_refresh, provider_snapshot, provider_state


class GuestCheckForm(forms.Form):
    email = forms.EmailField(required=False, max_length=254)
    country = forms.ChoiceField(required=False)
    phone = forms.CharField(required=False, max_length=32)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["country"].choices = GuestDetailsForm().fields["guest_phone_country"].choices
        self.fields["country"].error_messages["invalid_choice"] = "اختر دولة جوال صحيحة."

    def clean_phone(self):
        value = self.cleaned_data["phone"]
        if not value:
            return ""
        try:
            return normalize_booking_phone(
                value, default_region=self.cleaned_data.get("country") or "SA"
            )
        except InvalidPhoneNumber as exc:
            raise forms.ValidationError("تحقق من رقم الجوال والدولة المختارة.") from exc

    def clean(self):
        data = super().clean()
        if not data.get("email") and not data.get("phone"):
            raise forms.ValidationError("أدخل البريد الإلكتروني أو رقم الجوال لفحص الضيف.")
        return data


@operations_access_required
@translation.override("ar")
@require_POST
def guest_classification_check(request):
    if len(request.body) > 4096:
        return JsonResponse({"detail": "invalid_request"}, status=400)
    reference = None
    if request.POST.get("booking_id"):
        try:
            booking_id = uuid.UUID(request.POST["booking_id"])
        except ValueError:
            return JsonResponse({"detail": "invalid_request"}, status=400)
        booking = get_object_or_404(Reservation, pk=booking_id, is_test=False)
        reference = booking.hostaway_reservation_id
    else:
        form = GuestCheckForm(request.POST)
        if not form.is_valid():
            return JsonResponse({"detail": "invalid_guest", "errors": form.errors}, status=400)
    ensure_refresh(force=request.POST.get("refresh") == "1")
    snapshot = provider_snapshot()
    result = (
        classify_reference(reference, snapshot)
        if request.POST.get("booking_id")
        else classify_prospective(form.cleaned_data["email"], form.cleaned_data["phone"], snapshot)
    )
    state = provider_state()
    response = JsonResponse({**state, "classification": asdict(result)})
    response["Cache-Control"] = "private, no-store"
    return response
