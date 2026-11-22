"""Optional, booking-scoped service-message choices. Never a sending mechanism."""

import uuid

from django.conf import settings
from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import translation
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_post_parameters

from apps.accounts.models import VerifiedPhone
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number

from .models import BookingWhatsAppChoice, DayUseStay, MorningStay

VERSION = "booking-whatsapp-service-v1"
COPY = {
    "ar": {
        "title": "تحديثات الحجز عبر واتساب",
        "choice": (
            "أرغب في تلقي تحديثات هذا الحجز من Luxury Smart Apartments عبر WhatsApp "
            "على رقم جوالي الموثق، بما يشمل تأكيد الحجز وتعليمات الإقامة وتحديثات السداد والخروج."
        ),
        "note": (
            "اختياري، ولا يؤثر في الحجز أو الدفع. لا يشمل العروض أو الرسائل الترويجية. "
            "يمكنك سحب الموافقة هنا في أي وقت."
        ),
        "save": "حفظ اختياري",
        "withdraw": "سحب الموافقة",
        "saved": "تم حفظ اختيارك دون إرسال رسالة.",
        "active": "الموافقة مفعّلة لهذا الحجز.",
        "inactive": "لا توجد موافقة فعّالة لهذا الحجز.",
        "stale": "تغير اختيارك أو بيانات الحجز. حدّث الصفحة قبل الحفظ.",
    },
    "en": {
        "title": "Booking updates on WhatsApp",
        "choice": (
            "I want Luxury Smart Apartments to send updates for this booking on WhatsApp "
            "to my verified mobile, including booking confirmation, stay instructions, "
            "payment updates and checkout."
        ),
        "note": (
            "Optional and does not affect your booking or payment. It excludes offers and "
            "promotional messages. You can withdraw consent here at any time."
        ),
        "save": "Save my choice",
        "withdraw": "Withdraw consent",
        "saved": "Your choice was saved. No message was sent.",
        "active": "Consent is active for this booking.",
        "inactive": "There is no active consent for this booking.",
        "stale": "Your choice or booking changed. Refresh this page before saving.",
    },
    "fr": {
        "title": "Actualités de la réservation sur WhatsApp",
        "choice": (
            "Je souhaite recevoir de Luxury Smart Apartments les mises à jour de cette "
            "réservation sur WhatsApp, sur mon mobile vérifié : confirmation, consignes "
            "du séjour, paiement et départ."
        ),
        "note": (
            "Facultatif, sans effet sur la réservation ou le paiement. Cela exclut les offres "
            "et messages promotionnels. Vous pouvez retirer votre consentement ici à tout moment."
        ),
        "save": "Enregistrer mon choix",
        "withdraw": "Retirer le consentement",
        "saved": "Votre choix est enregistré. Aucun message envoyé.",
        "active": "Le consentement est actif pour cette réservation.",
        "inactive": "Aucun consentement actif pour cette réservation.",
        "stale": "Votre choix ou réservation a changé. Actualisez la page avant d’enregistrer.",
    },
}


def phone_digest(phone):
    return salted_hmac("booking-whatsapp-choice-phone.v1", phone, algorithm="sha256").hexdigest()


def subject_rows(stay, scenario):
    return BookingWhatsAppChoice.objects.filter(
        **{
            "morning_stay" if scenario == "07" else "day_use_stay": stay,
        }
    )


def consent_facts(stay, scenario):
    empty = {
        "service": None,
        "marketing": None,
        "purpose": None,
        "evidence_ref": None,
        "revoked": None,
    }
    if not getattr(settings, "BOOKING_WHATSAPP_CHOICE_ENABLED", False):
        return empty
    row = subject_rows(stay, scenario).order_by("-recorded_at", "-pk").first()
    if not row:
        return empty
    try:
        digest = phone_digest(normalize_phone_number(stay.guest_phone))
    except InvalidPhoneNumber:
        return empty
    binding = VerifiedPhone.objects.filter(
        user_id=row.user_id, version=row.binding_version, user__is_active=True
    ).first()
    if not binding or row.phone_digest != digest or phone_digest(binding.phone) != digest:
        return empty
    valid = row.booking_revision == stay.communication_revision and row.text_version == VERSION
    # A latest withdrawal remains authoritative even after a booking revision.
    if not valid and row.granted:
        return empty
    return {
        "service": row.granted,
        "marketing": None,
        "purpose": "booking_updates",
        "evidence_ref": "site01-wa-choice:" + str(row.pk),
        "revoked": not row.granted,
        "channel": "whatsapp",
        "recorded_at": row.recorded_at.isoformat(),
        "text_version": row.text_version,
        "source": "verified_guest_booking_portal",
    }


def choice_context(request, stay, scenario):
    if not getattr(settings, "BOOKING_WHATSAPP_CHOICE_ENABLED", False):
        return {}
    language = stay.guest_language if stay.guest_language in COPY else "ar"
    return {
        "whatsapp_choice_url": reverse(
            "accounts:booking_whatsapp_choice", args=[scenario, stay.pk]
        ),
        "whatsapp_choice_label": COPY[language]["title"],
    }


@sensitive_post_parameters()
def preferences(request, scenario, stay_id):
    request._disable_google_integrations = True
    if not getattr(settings, "BOOKING_WHATSAPP_CHOICE_ENABLED", False) or scenario not in {
        "07",
        "08",
    }:
        raise Http404
    if not request.user.is_authenticated or not request.user.is_active:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.path)
    if request.method not in {"GET", "POST"}:
        return private(render(request, "404.html", status=405))
    model = MorningStay if scenario == "07" else DayUseStay
    with transaction.atomic():
        # Same lock order as source reads, then phone binding. No booking writes.
        stay = model.objects.select_for_update().filter(pk=stay_id).first()
        binding = VerifiedPhone.objects.select_for_update().filter(user=request.user).first()
        try:
            matches = stay and binding and normalize_phone_number(stay.guest_phone) == binding.phone
        except InvalidPhoneNumber:
            matches = False
        if not matches:
            raise Http404
        language = stay.guest_language if stay.guest_language in COPY else "ar"
        copy = COPY[language]
        latest = subject_rows(stay, scenario).order_by("-recorded_at", "-pk").first()
        latest_id = str(latest.pk) if latest else "none"
        context = {
            "copy": copy,
            "choice_version": VERSION,
            "latest_id": latest_id,
            "booking_revision": stay.communication_revision,
            "language": language,
            "active": consent_facts(stay, scenario)["service"] is True,
        }
        status = 200
        if request.method == "POST":
            if (
                len(request.body) > 4096
                or request.POST.get("version") != VERSION
                or request.POST.get("latest_id") != latest_id
                or request.POST.get("booking_revision") != str(stay.communication_revision)
                or request.POST.get("action") not in {"save", "withdraw"}
                or request.POST.get("service", "") not in {"", "yes"}
            ):
                context["error"], status = copy["stale"], 409
            else:
                granted = (
                    request.POST.get("action") == "save" and request.POST.get("service") == "yes"
                )
                # Repeated submissions do not create a second decision. A stale
                # tab cannot silently reinstate a subsequently withdrawn choice.
                if latest is None or latest.granted != granted or not context["active"] and granted:
                    BookingWhatsAppChoice.objects.create(
                        id=uuid.uuid4(),
                        user=request.user,
                        **{"morning_stay" if scenario == "07" else "day_use_stay": stay},
                        phone_digest=phone_digest(binding.phone),
                        binding_version=binding.version,
                        booking_revision=stay.communication_revision,
                        granted=granted,
                        text_version=VERSION,
                        language=language,
                        text_snapshot=copy["choice"],
                    )
                return private(HttpResponseRedirect(request.path))
        with translation.override(language):
            return private(
                render(request, "accounts/booking_whatsapp_choice.html", context, status=status)
            )


def private(result):
    result["Cache-Control"] = "private, no-store, max-age=0"
    result["Referrer-Policy"] = "no-referrer"
    result["X-Robots-Tag"] = "noindex, nofollow"
    return result
