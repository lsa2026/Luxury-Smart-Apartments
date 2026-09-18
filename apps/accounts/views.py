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
from .forms import (
    CustomerAuthenticationForm,
    CustomerRegistrationForm,
    EmailVerificationCodeForm,
)
from .models import profile_for
from .services import (
    claim_reservation,
    claim_reservations_for_verified_email,
    claimable_reference,
)
from .tokens import read_verification_token, verification_code_is_valid

CLAIM_PARAM = "claim"
RESEND_RATE_LIMIT_REQUESTS = 3
RESEND_RATE_LIMIT_WINDOW = 15 * 60
# The existing email-and-password forms deliberately keep using Django's local
# backend.  Social providers register a second backend, so newly created users
# must state this explicitly when their session is created.
LOCAL_AUTH_BACKEND = "django.contrib.auth.backends.ModelBackend"

_VERIFICATION_COPY = {
    "ar": {
        "title": "أدخل رمز التحقق",
        "instructions": "أرسلنا رمزًا من 6 أرقام إلى بريدك الإلكتروني. أدخله هنا لتأكيد حسابك.",
        "code_label": "رمز التحقق",
        "submit": "تأكيد الحساب",
        "resend": "إرسال رمز جديد",
        "hint": "الرمز صالح لمدة 15 دقيقة. تحقق من البريد غير المرغوب فيه إذا لم يصلك.",
        "invalid": "الرمز غير صحيح أو انتهت صلاحيته. اطلب رمزًا جديدًا وحاول مرة أخرى.",
        "resent": "تم إرسال رمز تحقق جديد إلى بريدك الإلكتروني.",
    },
    "en": {
        "title": "Enter your verification code",
        "instructions": "We sent a six-digit code to your email. Enter it here to confirm your account.",
        "code_label": "Verification code",
        "submit": "Confirm account",
        "resend": "Send a new code",
        "hint": "The code is valid for 15 minutes. Check your spam folder if it has not arrived.",
        "invalid": "The code is incorrect or expired. Request a new code and try again.",
        "resent": "A new verification code has been sent to your email.",
    },
    "fr": {
        "title": "Saisissez votre code de vérification",
        "instructions": "Nous avons envoyé un code à six chiffres à votre adresse e-mail. Saisissez-le pour confirmer votre compte.",
        "code_label": "Code de vérification",
        "submit": "Confirmer le compte",
        "resend": "Envoyer un nouveau code",
        "hint": "Le code est valable 15 minutes. Vérifiez vos courriers indésirables s’il n’est pas arrivé.",
        "invalid": "Le code est incorrect ou expiré. Demandez un nouveau code et réessayez.",
        "resent": "Un nouveau code de vérification a été envoyé à votre adresse e-mail.",
    },
}


def _active_language(request: HttpRequest) -> str:
    return (getattr(request, "LANGUAGE_CODE", "") or "ar").split("-")[0]


def _verification_copy(request: HttpRequest) -> dict[str, str]:
    return _VERIFICATION_COPY.get(_active_language(request), _VERIFICATION_COPY["ar"])


def _complete_email_verification(request: HttpRequest, user: object) -> None:
    profile = profile_for(user)
    if profile.is_email_verified:
        return
    profile.mark_verified()
    claim_reservations_for_verified_email(user)
    queue_welcome_email(user, language=_active_language(request))


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
        login(request, user, backend=LOCAL_AUTH_BACKEND)
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
        return redirect("accounts:verify_pending")


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
        login(request, user, backend=LOCAL_AUTH_BACKEND)
        messages.success(request, _("Welcome back."))
        _claim_after_authentication(request, user)
        if not profile_for(user).is_email_verified:
            return redirect("accounts:verify_pending")
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

    _complete_email_verification(request, user)
    messages.success(request, _("Your email address is confirmed."))
    if request.user.is_authenticated and request.user.pk == user.pk:
        return redirect("accounts:dashboard")
    return redirect("accounts:login")


@login_required(login_url="accounts:login")
def verify_pending(request: HttpRequest) -> HttpResponse:
    profile = profile_for(request.user)
    if profile.is_email_verified:
        return redirect("accounts:dashboard")
    return render(
        request,
        "accounts/verify_pending.html",
        {
            "profile": profile,
            "verification_form": EmailVerificationCodeForm(),
            "verification_copy": _verification_copy(request),
        },
    )


@login_required(login_url="accounts:login")
def verify_email_code(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("accounts:verify_pending")
    profile = profile_for(request.user)
    if profile.is_email_verified:
        return redirect("accounts:dashboard")
    copy = _verification_copy(request)
    form = EmailVerificationCodeForm(request.POST)
    if is_rate_limited(
        request,
        scope="account-email-verification-code",
        requests=5,
        window=settings.ACCOUNT_EMAIL_VERIFICATION_CODE_MAX_AGE_SECONDS,
    ) or not form.is_valid() or not verification_code_is_valid(
        user_pk=request.user.pk,
        email=request.user.email,
        issued_at=profile.verification_sent_at,
        submitted_code=form.cleaned_data.get("code", ""),
    ):
        if "code" not in form.errors:
            form.add_error("code", copy["invalid"])
        return render(
            request,
            "accounts/verify_pending.html",
            {
                "profile": profile,
                "verification_form": form,
                "verification_copy": copy,
            },
            status=400,
        )
    _complete_email_verification(request, request.user)
    messages.success(request, _("Your email address is confirmed."))
    return redirect("accounts:dashboard")


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
    messages.success(request, _verification_copy(request)["resent"])
    return redirect("accounts:verify_pending")
