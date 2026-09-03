"""Customer registration, sign-in, and booking dashboard."""

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views import View

from apps.reservations.models import Reservation

from .forms import CustomerAuthenticationForm, CustomerRegistrationForm
from .services import claim_reservation, claimable_reference

CLAIM_PARAM = "claim"


def _claim_after_authentication(request: HttpRequest, user: object) -> None:
    """Move the booking this session proved into the account just used.

    Called for sign-up and sign-in alike, so a guest who booked first and
    registered afterwards finds the stay waiting on the dashboard instead of an
    empty page.
    """
    reference = request.POST.get(CLAIM_PARAM) or request.GET.get(CLAIM_PARAM) or ""
    reservation = claim_reservation(request, user, reference)
    if reservation is not None:
        messages.success(
            request,
            _("Booking %(reference)s is now saved to your account.")
            % {"reference": reservation.public_reference},
        )


def _safe_next(request: HttpRequest, fallback: str) -> str:
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return reverse(fallback)


class RegisterView(View):
    http_method_names = ["get", "post"]

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("accounts:dashboard")
        return render(
            request,
            "accounts/register.html",
            {
                "form": CustomerRegistrationForm(),
                "claim": claimable_reference(request, request.GET.get(CLAIM_PARAM, "")),
                "next": request.GET.get("next", ""),
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("accounts:dashboard")
        form = CustomerRegistrationForm(request.POST)
        claim = request.POST.get(CLAIM_PARAM, "")
        if not form.is_valid():
            return render(
                request,
                "accounts/register.html",
                {
                    "form": form,
                    "claim": claimable_reference(request, claim),
                    "next": request.POST.get("next", ""),
                },
                status=400,
            )
        user = form.save()
        login(request, user)
        messages.success(request, _("Your account is ready."))
        _claim_after_authentication(request, user)
        return redirect(_safe_next(request, "accounts:dashboard"))


class LoginView(View):
    http_method_names = ["get", "post"]

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("accounts:dashboard")
        return render(
            request,
            "accounts/login.html",
            {
                "form": CustomerAuthenticationForm(request),
                "next": request.GET.get("next", ""),
                "claim": claimable_reference(request, request.GET.get(CLAIM_PARAM, "")),
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("accounts:dashboard")
        form = CustomerAuthenticationForm(request, data=request.POST)
        if not form.is_valid():
            return render(
                request,
                "accounts/login.html",
                {
                    "form": form,
                    "next": request.POST.get("next", ""),
                    "claim": claimable_reference(request, request.POST.get(CLAIM_PARAM, "")),
                },
                status=400,
            )
        user = form.get_user()
        login(request, user)
        messages.success(request, _("Welcome back."))
        _claim_after_authentication(request, user)
        return redirect(_safe_next(request, "accounts:dashboard"))


class LogoutView(View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        logout(request)
        messages.success(request, _("You have signed out securely."))
        return redirect("core:home")


@login_required(login_url="accounts:login")
def dashboard(request: HttpRequest) -> HttpResponse:
    reservations = (
        Reservation.objects.select_related("property", "booking_intent")
        .filter(booking_intent__customer=request.user)
        .order_by("-created_at")
    )
    return render(request, "accounts/dashboard.html", {"reservations": reservations})
