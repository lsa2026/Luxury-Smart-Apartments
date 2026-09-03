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
        return render(request, "accounts/register.html", {"form": CustomerRegistrationForm()})

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("accounts:dashboard")
        form = CustomerRegistrationForm(request.POST)
        if not form.is_valid():
            return render(request, "accounts/register.html", {"form": form}, status=400)
        user = form.save()
        login(request, user)
        messages.success(request, _("Your account is ready."))
        return redirect(_safe_next(request, "accounts:dashboard"))


class LoginView(View):
    http_method_names = ["get", "post"]

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("accounts:dashboard")
        return render(
            request,
            "accounts/login.html",
            {"form": CustomerAuthenticationForm(request), "next": request.GET.get("next", "")},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("accounts:dashboard")
        form = CustomerAuthenticationForm(request, data=request.POST)
        if not form.is_valid():
            return render(
                request,
                "accounts/login.html",
                {"form": form, "next": request.POST.get("next", "")},
                status=400,
            )
        login(request, form.get_user())
        messages.success(request, _("Welcome back."))
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
