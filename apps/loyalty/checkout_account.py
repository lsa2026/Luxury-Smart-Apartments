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

from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingIntent, Reservation
from apps.reservations.security import session_owns

from .views import _actions_allowed, _verified, interface_enabled

PENDING = "loyalty_checkout_account_returns"
TEXT = {
    "ar": {
        "title": "لديك عضوية أو حساب سابق؟",
        "help": (
            "سجّل الدخول بجوالك الموثق لربط الحجز بعضويتك واكتساب مكافآتك أو استخدام نقاطك. "
            "سنحتفظ بالشقة والتواريخ والبيانات ونُعيدك إلى الطلب نفسه."
        ),
        "identity": (
            "الجوال الموثق هو وسيلة التعرف الأساسية. لا تحتاج إلى فتح بريدك للدخول بالجوال، "
            "ولا تُصرف نقاطك تلقائيًا."
        ),
        "login": "متابعة بجوالي لربط الحجز والمكافآت",
        "link": "اربط الحجز بحسابي ومكافآتي",
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
        "title": "Already a member or returning guest?",
        "help": (
            "Sign in with your verified mobile to link this stay, earn rewards or use points. "
            "We will keep your apartment, "
            "dates and details and bring you back to this booking request."
        ),
        "identity": (
            "Your verified mobile is the primary identifier; email access is not required. "
            "Signing in never spends points automatically."
        ),
        "login": "Continue with my mobile for bookings and rewards",
        "link": "Link this booking to my account and rewards",
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
        "title": "Déjà membre ou déjà client ?",
        "help": (
            "Connectez-vous avec votre mobile vérifié pour rattacher ce séjour, "
            "gagner ou utiliser des points. Votre appartement, vos dates et vos "
            "coordonnées seront conservés et vous retrouverez cette demande de réservation."
        ),
        "identity": (
            "Votre mobile vérifié est l’identifiant principal ; l’accès à l’e-mail est inutile. "
            "La connexion ne dépense jamais vos points automatiquement."
        ),
        "login": "Continuer avec mon mobile pour mon séjour et mes points",
        "link": "Rattacher ce séjour à mon compte et à mes avantages",
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
    from apps.accounts.identity import booking_identity_matches

    if not user.is_active or not _verified(user):
        return False
    return booking_identity_matches(user, intent.guest_email, intent.guest_phone)


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
    from apps.accounts.phone_auth import ready

    route = "accounts:phone_login" if ready() else "accounts:login"
    return reverse(route) + "?" + urlencode({"next": target})


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
                request.session["guest_checkout_phone"] = intent.guest_phone
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
