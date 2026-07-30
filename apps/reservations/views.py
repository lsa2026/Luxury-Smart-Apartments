"""Public availability endpoint; all Hostaway calls remain server-side."""

from hashlib import sha256
from secrets import token_urlsafe

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views import View

from apps.properties.models import Property

from .forms import AvailabilitySearchForm
from .services.availability import (
    AvailabilityRequest,
    AvailabilityService,
    component_title_ar,
)


class AvailabilitySearchView(View):
    http_method_names = ["post"]
    service_class = AvailabilityService

    def post(self, request: HttpRequest) -> HttpResponse:
        if self._is_rate_limited(request):
            return render(
                request,
                "reservations/availability_result.html",
                {
                    "rate_limited": True,
                    "user_message": "تم تجاوز عدد محاولات التحقق. يرجى الانتظار قليلًا.",
                },
                status=429,
            )

        form = AvailabilitySearchForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                "reservations/availability_result.html",
                {"form": form},
                status=400,
            )

        property_obj = form.cleaned_data["property"]
        assert isinstance(property_obj, Property)
        availability_request = AvailabilityRequest(
            property=property_obj,
            check_in=form.cleaned_data["check_in"],
            check_out=form.cleaned_data["check_out"],
            guests=form.cleaned_data["guests"],
        )
        with self.service_class() as service:
            result = service.check(availability_request)

        components = []
        if result.quote:
            components = [
                {
                    "title": component_title_ar(component),
                    "amount": component.total if component.total is not None else component.value,
                }
                for component in result.quote.components
            ]
        return render(
            request,
            "reservations/availability_result.html",
            {
                "availability": result,
                "property": property_obj,
                "cover_image": property_obj.cover_image,
                "check_in": availability_request.check_in,
                "check_out": availability_request.check_out,
                "guests": availability_request.guests,
                "price_components": components,
            },
        )

    @staticmethod
    def _is_rate_limited(request: HttpRequest) -> bool:
        session_marker = request.session.get("availability_rate_marker")
        if not session_marker:
            request.session["availability_rate_marker"] = (
                request.session.session_key or token_urlsafe(18)
            )
            session_marker = request.session["availability_rate_marker"]
        remote_address = request.META.get("REMOTE_ADDR", "")
        digest = sha256(
            f"{settings.SECRET_KEY}:{session_marker}:{remote_address}".encode()
        ).hexdigest()
        key = f"availability:rate:v1:{digest}"
        if cache.add(key, 1, timeout=settings.AVAILABILITY_RATE_LIMIT_WINDOW):
            return False
        try:
            attempts = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=settings.AVAILABILITY_RATE_LIMIT_WINDOW)
            attempts = 1
        return attempts > settings.AVAILABILITY_RATE_LIMIT_REQUESTS
