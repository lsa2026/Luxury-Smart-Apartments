"""Customer registration, sign-in, and booking dashboard."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views import View

from apps.reservations.models import Reservation
from apps.reservations.security import is_rate_limited

from .emails import queue_verification_email, queue_welcome_email
from .forms import CustomerAuthenticationForm, CustomerRegistrationForm
from .models import profile_for
from .services import claim_reservation, claimable_reference
from .tokens import read_verification_token

CLAIM_PARAM = "claim"
RESEND_RATE_LIMIT_REQUESTS = 3
RESEND_RATE_LIMIT_WINDOW = 15 * 60


def _active_language(request: HttpRequest) -> str:
    return (getattr(request, "LANGUAGE_CODE", "") or "ar").split("-")[0]


def _send_verification(request: HttpRequest, user: object) -> None:
    profile = profile_for(user)
    if profile.is_email_verified:
        return
    profile.verification_sent_for = user.email
    profile.verification_sent_at = timezone.now()
    profile.save(update_fields=["verification_sent_for", "verification_sent_at", "updated_at"])
    queue_verification_email(user, language=_active_language(request))


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
        _send_verification(request, user)
        # Only promise an email the site can actually deliver. While delivery is
        # off the row is still queued, so the audit trail is unbroken and the
        # backlog sends once a provider is configured.
        messages.success(
            request,
            _("Your account is ready. Check your inbox to confirm your email address.")
            if settings.EMAIL_DELIVERY_ENABLED
            else _("Your account is ready."),
        )
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


def verify_email(request: HttpRequest, token: str) -> HttpResponse:
    """Confirm an address from a signed link.

    Deliberately not restricted to the signed-in visitor: people open these
    links in whichever browser their mail client hands them. The signature is
    the proof, so it works from any session, and confirming an address that is
    already confirmed is treated as success rather than an error.
    """
    payload = read_verification_token(token)
    if payload is None:
        messages.error(
            request,
            _("This confirmation link is no longer valid. Request a new one."),
        )
        return redirect("accounts:verify_pending")

    user_pk, email = payload
    user = get_user_model().objects.filter(pk=user_pk).first()
    # The address must still be the one the link was issued for, otherwise an
    # old link would confirm an address the customer has since changed.
    if user is None or (user.email or "").strip().casefold() != email:
        messages.error(
            request,
            _("This confirmation link is no longer valid. Request a new one."),
        )
        return redirect("accounts:verify_pending")

    profile = profile_for(user)
    if not profile.is_email_verified:
        profile.mark_verified()
        queue_welcome_email(user, language=_active_language(request))
    messages.success(request, _("Your email address is confirmed."))
    if request.user.is_authenticated and request.user.pk == user.pk:
        return redirect("accounts:dashboard")
    return redirect("accounts:login")


@login_required(login_url="accounts:login")
def verify_pending(request: HttpRequest) -> HttpResponse:
    profile = profile_for(request.user)
    if profile.is_email_verified:
        return redirect("accounts:dashboard")
    return render(request, "accounts/verify_pending.html", {"profile": profile})


@login_required(login_url="accounts:login")
def resend_verification(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("accounts:verify_pending")
    profile = profile_for(request.user)
    if profile.is_email_verified:
        return redirect("accounts:dashboard")
    if is_rate_limited(
        request,
        scope="account-verify-resend",
        requests=RESEND_RATE_LIMIT_REQUESTS,
        window=RESEND_RATE_LIMIT_WINDOW,
    ):
        messages.error(
            request,
            _("You have asked for several links recently. Please wait a few minutes."),
        )
        return redirect("accounts:verify_pending")
    _send_verification(request, request.user)
    messages.success(request, _("A new confirmation email is on its way."))
    return redirect("accounts:verify_pending")
