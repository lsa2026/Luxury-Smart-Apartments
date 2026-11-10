"""Local formatting only; no identity lookup, provider call or booking write."""

import phonenumbers
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_booking_phone

from .security import is_ip_rate_limited


@never_cache
@require_POST
def booking_phone_format(request):
    if len(request.body) > 4096:
        return JsonResponse({"detail": "invalid_phone"}, status=400)
    if is_ip_rate_limited(request, scope="booking-phone-format", requests=60, window=300):
        return JsonResponse({"detail": "temporarily_unavailable"}, status=429)
    try:
        value = request.POST.get("phone", "")
        if len(value) > 32:
            raise InvalidPhoneNumber
        phone = normalize_booking_phone(value, default_region=request.POST.get("country") or "SA")
        region = phonenumbers.region_code_for_number(phonenumbers.parse(phone, None))
    except InvalidPhoneNumber:
        return JsonResponse({"detail": "invalid_phone"}, status=400)
    return JsonResponse({"phone": phone, "country": region})
