"""Resume one session-owned, unpaid checkout after any existing sign-in method.

This is not historical booking recovery. A CSRF-protected action records intent
to link this one request, and the return rechecks session and verified identity.
It never changes guest contacts, price, consent, points or payment records.
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.accounts.models import VerifiedPhone
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingIntent, Reservation
from apps.reservations.security import session_owns

from .views import _actions_allowed, _verified, interface_enabled

PENDING = "loyalty_checkout_account_returns"
TEXT = {
    "ar": {
        "title": "لديك نقاط مكافآت؟",
        "help": (
            "سجّل الدخول لاستخدام نقاطك في هذا الحجز. "
            "سنحتفظ بالشقة والتواريخ والبيانات ونُعيدك إلى الطلب نفسه."
        ),
        "identity": (
            "استخدم حسابًا يطابق بريد الحجز أو جواله الموثّق. تسجيل الدخول لا يصرف نقاطك تلقائيًا."
        ),
        "login": "سجّل الدخول لاستخدام نقاطك",
        "link": "استخدم نقاط حسابي لهذا الحجز",
        "linked": "تم ربط طلب الحجز بحسابك. يمكنك الآن اختيار النقاط قبل الدفع.",
        "mismatch": (
            "تعذر ربط الطلب بهذا الحساب. استخدم الحساب المطابق لبريد الحجز "
            "أو جواله الموثّق، أو تابع دون نقاط."
        ),
        "unavailable": (
            "لا يمكن ربط الطلب الآن؛ ربما انتهت صلاحيته أو بدأت عملية الدفع. "
            "لم تتغير بيانات الحجز أو نقاطك."
        ),
    },
    "en": {
        "title": "Have reward points?",
        "help": (
            "Sign in to use your points for this stay. We will keep your apartment, "
            "dates and details and bring you back to this booking request."
        ),
        "identity": (
            "Use an account matching the booking email or a verified mobile number. "
            "Signing in never spends points automatically."
        ),
        "login": "Sign in to use your points",
        "link": "Use my account’s points for this booking",
        "linked": (
            "This booking request is now linked to your account. Choose your points before paying."
        ),
        "mismatch": (
            "We could not link this request to this account. Use the account matching "
            "the booking email or verified mobile number, or continue without points."
        ),
        "unavailable": (
            "This request cannot be linked now: it may have expired or payment may have started. "
            "Your booking details and points are unchanged."
        ),
    },
    "fr": {
        "title": "Vous avez des points ?",
        "help": (
            "Connectez-vous pour utiliser vos points. Votre appartement, vos dates et vos "
            "coordonnées seront conservés et vous retrouverez cette demande de réservation."
        ),
        "identity": (
            "Utilisez le compte correspondant à l’e-mail de réservation ou au mobile vérifié. "
            "La connexion ne dépense jamais vos points automatiquement."
        ),
        "login": "Me connecter pour utiliser mes points",
        "link": "Utiliser les points de mon compte",
        "linked": (
            "Cette demande est maintenant liée à votre compte. "
            "Choisissez vos points avant de payer."
        ),
        "mismatch": (
            "Impossible de rattacher cette demande à ce compte. Utilisez le compte correspondant "
            "à l’e-mail de réservation ou au mobile vérifié, ou continuez sans points."
        ),
        "unavailable": (
            "Cette demande ne peut plus être rattachée : elle a peut-être expiré ou "
            "le paiement a commencé. Vos informations et vos points sont inchangés."
        ),
    },
}


def _text(intent):
    return TEXT.get(intent.language, TEXT["ar"])


def _payable(intent):
    return (
        intent.status == BookingIntent.Status.AWAITING_PAYMENT
        and intent.expires_at > timezone.now()
        and intent.currency == "SAR"
        and intent.payment_amount_sar == intent.total_price
    )


def _identity_matches(user, intent):
    if not user.is_active or not _verified(user):
        return False
    if user.email and user.email.strip().casefold() == intent.guest_email.strip().casefold():
        return True
    try:
        phone = normalize_phone_number(intent.guest_phone)
    except InvalidPhoneNumber:
        return False
    # An editable profile contact is not a verified sign-in credential.
    return VerifiedPhone.objects.filter(user=user, phone=phone).exists()


def account_context(request, intent):
    if (
        not interface_enabled(request.user)
        or not _actions_allowed(request)
        or not _payable(intent)
        or not session_owns(request, intent.session_key_hash)
        or intent.customer_id is not None
    ):
        return {}
    return {"checkout_account": {"text": _text(intent)}}


def _payment_started(intent):
    attempts = (
        PaymentAttempt.objects.filter(booking_intent=intent)
        .exclude(status__in=["failed", "cancelled", "expired"])
        .exists()
    )
    reservations = Reservation.objects.filter(booking_intent=intent)
    return attempts or any(
        row.normalized_status
        not in {Reservation.Status.AWAITING_PAYMENT, Reservation.Status.READY_FOR_HOSTAWAY}
        or any(row.loyalty_state.get(key) for key in ("hold", "redemption", "checkout_frozen"))
        for row in reservations
    )


def _login_url(reference):
    # Never accept an arbitrary client-supplied return destination.
    target = reverse("reservations:booking_account", args=[reference])
    return reverse("accounts:login") + "?" + urlencode({"next": target})


@never_cache
@require_http_methods(["GET", "POST"])
def booking_account(request, public_reference):
    if not interface_enabled(request.user) or not _actions_allowed(request):
        raise Http404
    with transaction.atomic():
        intent = (
            BookingIntent.objects.select_for_update()
            .filter(public_reference=public_reference)
            .first()
        )
        if intent is None or not session_owns(request, intent.session_key_hash):
            raise Http404
        pending = request.session.get(PENDING, {})
        if not isinstance(pending, dict):
            pending = {}
        now = int(timezone.now().timestamp())
        pending = {k: v for k, v in pending.items() if isinstance(v, int) and v > now}
        if request.method == "GET" and public_reference not in pending:
            raise Http404
        if not _payable(intent) or (intent.customer_id is None and _payment_started(intent)):
            messages.warning(request, _text(intent)["unavailable"])
        elif not request.user.is_authenticated:
            # Session data survives normal authentication key rotation. Bound the
            # map and deadline; signing in never extends the underlying quote.
            if request.method == "POST":
                pending = dict(list(pending.items())[-4:])
                pending[public_reference] = min(now + 1200, int(intent.expires_at.timestamp()))
                request.session[PENDING] = pending
            return redirect(_login_url(public_reference))
        elif intent.customer_id == request.user.pk:
            pass
        elif intent.customer_id is not None or not _identity_matches(request.user, intent):
            messages.warning(request, _text(intent)["mismatch"])
        else:
            # Compare-and-set also protects against historical recovery linking
            # this intent while the checkout waits for its row lock.
            changed = BookingIntent.objects.filter(pk=intent.pk, customer__isnull=True).update(
                customer=request.user
            )
            if changed:
                messages.success(request, _text(intent)["linked"])
            else:
                messages.warning(request, _text(intent)["mismatch"])
        pending.pop(public_reference, None)
        request.session[PENDING] = pending
    return redirect("reservations:intent_detail", public_reference=public_reference)
