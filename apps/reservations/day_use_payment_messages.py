"""Owner-requested pre-payment WhatsApp notices, isolated from paid admission."""

import hashlib
import json
from datetime import UTC, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.notifications.services.ultramsg import (
    UltraMsgClient,
    UltraMsgConfigurationError,
    UltraMsgConnectionError,
    UltraMsgResponseError,
)
from apps.payments.day_use_payments import public_url
from apps.properties.models import Property

from .day_use_stays import (
    UNRESOLVED,
    DayUseStayError,
    active_stays,
    local_inflight_conflict,
    validate_window,
    verify_window,
)
from .models import DayUsePaymentLinkDelivery, DayUseStay

SALT = "day_use-payment-whatsapp.v1"
LABELS = {
    "waiting_core": "بانتظار تسليم رسائل الحجز الثلاث قبل إرسال رابط الدفع تلقائيًا",
    "queued": "بانتظار الإرسال",
    "sending": "جارٍ الإرسال — لا تعِد الإرسال قبل حسم النتيجة",
    "accepted": "قبلت خدمة واتساب الرسالة؛ لا يثبت ذلك قراءة الضيف",
    "uncertain": "نتيجة الإرسال غير محسومة؛ راجع واتساب قبل إعادة الإرسال",
    "failed": "تعذر الإرسال؛ يمكنك المحاولة من جديد",
    "expired": "لم تُرسل الرسالة لعدم تحقق التوفر أو صلاحية الطلب؛ أعد الفحص",
}


def configured():
    return bool(
        settings.HYPERPAY_ENABLED
        and settings.ULTRAMSG_ENABLED
        and settings.ULTRAMSG_INSTANCE_ID
        and settings.ULTRAMSG_TOKEN
    )


def fingerprint(stay):
    values = {
        key: getattr(stay, key)
        for key in ("property_id", "guest_name", "guest_phone", "guest_language", "environment")
    }
    values["amount_sar"] = f"{stay.amount_sar:.2f}"
    for key in ("starts_at", "ends_at", "expires_at"):
        values[key] = getattr(stay, key).astimezone(UTC).isoformat()
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def require_unpaid(stay):
    if not configured() or stay.environment != settings.HYPERPAY_ENVIRONMENT:
        raise DayUseStayError("إرسال رابط الدفع عبر واتساب غير مفعّل لهذا الطلب.")
    from .day_use_hostaway import booking_state

    if (
        not settings.DAY_USE08_ENABLED
        or stay.ends_at <= timezone.now()
        or booking_state(stay).get("state") != "confirmed"
    ):
        raise DayUseStayError("الحجز النهاري غير متاح للسداد.")
    if stay.status not in {"open", "expired"} or stay.paid_at:
        raise DayUseStayError("الطلب مدفوع أو ملغى أو يحتاج حسم حالة الدفع؛ لم يُرسل رابط جديد.")
    if stay.attempts.filter(status__in=(*UNRESOLVED, "succeeded")).exists():
        raise DayUseStayError(
            "توجد محاولة دفع غير محسومة أو ناجحة؛ تحقق من الدفع قبل إعادة الإرسال."
        )
    from .day_use_stays import property_zone

    validate_window(
        stay.starts_at,
        stay.ends_at,
        zone=property_zone(stay.property),
        allow_started=True,
        allow_elapsed=False,
    )
    try:
        normalize_phone_number(stay.guest_phone)
    except InvalidPhoneNumber as exc:
        raise DayUseStayError("رقم واتساب الضيف غير صحيح.") from exc


def render_message(stay, *, preview=False):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(stay.time_zone)
    start, end = (timezone.localtime(value, zone) for value in (stay.starts_at, stay.ends_at))
    lang = stay.guest_language
    name = getattr(stay.property, f"name_{lang}", "") or stay.property.name_en
    ref = "DU-" + str(stay.pk)[:8]
    url = public_url(stay)
    labels = {
        "ar": (
            "تفاصيل الحجز النهاري",
            "الضيف",
            "الإجمالي",
            "رابط الدفع الآمن",
            "صالح حتى",
            "التأكيد وتعليمات الدخول بعد نجاح الدفع.",
        ),
        "en": (
            "DayUse stay details",
            "Guest",
            "Total",
            "Secure payment",
            "Valid until",
            "Confirmation and access instructions follow successful payment.",
        ),
        "fr": (
            "Détails du séjour en journée",
            "Client",
            "Total",
            "Paiement sécurisé",
            "Valable jusqu’à",
            "Confirmation et accès après paiement réussi.",
        ),
    }.get(
        lang,
        (
            "تفاصيل الحجز النهاري",
            "الضيف",
            "الإجمالي",
            "رابط الدفع الآمن",
            "صالح حتى",
            "التأكيد وتعليمات الدخول بعد نجاح الدفع.",
        ),
    )
    title, guest, total, link, _until, note = labels
    from .day_use_hostaway import hostaway_first

    if hostaway_first(stay):
        note = {
            "ar": "الحجز مؤكد في هوستاوي؛ هذا الرابط لسداد المبلغ فقط.",
            "en": "Your booking is confirmed in Hostaway; this link is for payment only.",
            "fr": (
                "Votre réservation est confirmée dans Hostaway ; "
                "ce lien sert uniquement au paiement."
            ),
        }.get(lang, "الحجز مؤكد في هوستاوي؛ هذا الرابط لسداد المبلغ فقط.")
    arrival, departure = {
        "ar": ("الدخول", "الخروج"),
        "en": ("Check-in", "Check-out"),
        "fr": ("Arrivée", "Départ"),
    }.get(lang, ("الدخول", "الخروج"))
    return (
        f"*{title}*\n\n{guest}: {stay.guest_name}\n🏡 {name}\n"
        f"{ref}\n📅 {start:%Y-%m-%d}\n🕒 {arrival}: {start:%H:%M}\n"
        f"🕒 {departure}: {end:%H:%M}\n{stay.time_zone}\n"
        f"💳 {total}: SAR {stay.amount_sar:.2f}\n\n*{link}*\n{url}\n\n"
        f"{note}"
    )


def send_link(delivery_id):
    delivery = DayUsePaymentLinkDelivery.objects.select_related("stay__property").get(
        pk=delivery_id
    )
    if delivery.state != "queued":
        return delivery.state
    stay = delivery.stay
    try:
        require_unpaid(stay)
        from .day_use_loyalty import core_delivered

        if not core_delivered(stay, stay.communication_revision):
            raise DayUseStayError("لم يثبت تسليم الرسائل الثلاث.")
        if fingerprint(stay) != delivery.fingerprint:
            raise DayUseStayError("stale")
        verify_window(
            stay.property,
            stay.starts_at,
            stay.ends_at,
            guests=stay.guests,
            allow_started=True,
            allow_elapsed=False,
            linked_stay=stay
            if (stay.availability_evidence or {}).get("hostaway_day_use")
            else None,
        )
    except Exception:
        DayUsePaymentLinkDelivery.objects.filter(pk=delivery_id, state="queued").update(
            state="expired", last_error="eligibility_not_verified"
        )
        return "expired"
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=stay.property_id)
        stay = DayUseStay.objects.select_for_update().select_related("property").get(pk=stay.pk)
        delivery = DayUsePaymentLinkDelivery.objects.select_for_update().get(pk=delivery_id)
        if delivery.state != "queued":
            return delivery.state
        try:
            require_unpaid(stay)
            if (
                fingerprint(stay) != delivery.fingerprint
                or local_inflight_conflict(stay.property, stay.starts_at, stay.ends_at)
                or active_stays(
                    stay.property,
                    stay.starts_at,
                    stay.ends_at + timedelta(hours=3),
                    exclude=stay.pk,
                ).exists()
            ):
                raise DayUseStayError("stale")
        except DayUseStayError:
            delivery.state, delivery.last_error = "expired", "eligibility_changed"
            delivery.save(update_fields=["state", "last_error"])
            return delivery.state
        delivery.state = "sending"
        delivery.save(update_fields=["state"])
        body = render_message(stay)
        recipient = normalize_phone_number(stay.guest_phone)
    try:
        with UltraMsgClient() as provider:
            result = provider.send_text(
                recipient=recipient, body=body, reference_id="day-use-pay-" + str(delivery.pk)
            )
        state, error, provider_id = "accepted", "", str(result.get("id") or "")
    except UltraMsgConfigurationError:
        state, error, provider_id = "failed", "provider_disabled", ""
    except UltraMsgResponseError:
        state, error, provider_id = "failed", "provider_rejected", ""
    except UltraMsgConnectionError:
        state, error, provider_id = "uncertain", "provider_result_uncertain", ""
    except Exception:
        state, error, provider_id = "uncertain", "provider_result_uncertain", ""
    DayUsePaymentLinkDelivery.objects.filter(pk=delivery_id, state="sending").update(
        state=state, last_error=error, provider_id=provider_id, completed_at=timezone.now()
    )
    return state
