"""Owner-requested pre-payment WhatsApp notices, isolated from paid admission."""

import hashlib
import json
import uuid
from datetime import UTC, timedelta

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.notifications.services.ultramsg import (
    UltraMsgClient,
    UltraMsgConfigurationError,
    UltraMsgConnectionError,
    UltraMsgResponseError,
)
from apps.payments.morning_payments import public_url
from apps.properties.models import Property

from .models import MorningPaymentLinkDelivery, MorningStay
from .morning_stays import (
    UNRESOLVED,
    MorningStayError,
    active_stays,
    local_inflight_conflict,
    validate_window,
    verify_window,
)

SALT = "morning-payment-whatsapp.v1"
LABELS = {
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
        raise MorningStayError("إرسال رابط الدفع عبر واتساب غير مفعّل لهذا الطلب.")
    if stay.status not in {"open", "expired"} or stay.paid_at:
        raise MorningStayError("الطلب مدفوع أو ملغى أو يحتاج حسم حالة الدفع؛ لم يُرسل رابط جديد.")
    if stay.attempts.filter(status__in=(*UNRESOLVED, "succeeded")).exists():
        raise MorningStayError(
            "توجد محاولة دفع غير محسومة أو ناجحة؛ تحقق من الدفع قبل إعادة الإرسال."
        )
    from .morning_stays import property_zone

    validate_window(stay.starts_at, stay.ends_at, zone=property_zone(stay.property))
    try:
        normalize_phone_number(stay.guest_phone)
    except InvalidPhoneNumber as exc:
        raise MorningStayError("رقم واتساب الضيف غير صحيح.") from exc


def render_message(stay, *, preview=False):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(stay.time_zone)
    start, end, expiry = (
        timezone.localtime(value, zone) for value in (stay.starts_at, stay.ends_at, stay.expires_at)
    )
    if preview:
        expiry = timezone.localtime(
            min(timezone.now() + timedelta(minutes=5), stay.starts_at), zone
        )
    lang = stay.guest_language
    name = getattr(stay.property, f"name_{lang}", "") or stay.property.name_en
    ref = "AM-" + str(stay.pk)[:8]
    url = public_url(stay)
    when = f"{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} ({stay.time_zone})"
    if lang == "en":
        return (
            f"☀️ *Morning booking request confirmation*\n*Luxury Smart Apartments 🌿*\n\n"
            f"Hello {stay.guest_name},\nYour morning stay has been registered.\n"
            f"🏡 {name}\n📋 {ref}\n🕒 {when}\n💳 Total: SAR {stay.amount_sar:.2f}\n\n"
            f"🔗 Secure payment link:\n{url}\n\n"
            f"Pay before {expiry:%Y-%m-%d %H:%M} ({stay.time_zone}).\n"
            "Final confirmation follows successful payment. Check-in instructions and house rules "
            "will then be sent directly to you.\n🛎️ +966501205651"
        )
    if lang == "fr":
        return (
            f"☀️ *Confirmation de demande de séjour matinal*\n*Luxury Smart Apartments 🌿*\n\n"
            f"Bonjour {stay.guest_name},\nVotre demande a été enregistrée.\n"
            f"🏡 {name}\n📋 {ref}\n🕒 {when}\n💳 Total : {stay.amount_sar:.2f} SAR\n\n"
            f"🔗 Paiement sécurisé :\n{url}\n\n"
            f"Payez avant le {expiry:%Y-%m-%d %H:%M} ({stay.time_zone}).\n"
            "La confirmation définitive suit le paiement réussi. Les instructions d’arrivée et "
            "le règlement intérieur vous seront alors envoyés directement.\n🛎️ +966501205651"
        )
    return (
        f"☀️ *تأكيد طلب الحجز الصباحي*\n*Luxury Smart Apartments 🌿*\n\n"
        f"مرحبًا {stay.guest_name}،\nتم تسجيل حجزك الصباحي بالتفاصيل التالية:\n"
        f"🏡 {name}\n📋 المرجع: {ref}\n🕒 الدخول والخروج: {when}\n"
        f"💳 الإجمالي: {stay.amount_sar:.2f} ريال سعودي\n\n🔗 رابط السداد الآمن:\n{url}\n\n"
        f"يرجى إتمام الدفع قبل {expiry:%Y-%m-%d %H:%M} ({stay.time_zone}).\n"
        "يكتمل تأكيد الحجز بعد نجاح الدفع، وستصلك تعليمات الدخول والمنزل مباشرة.\n"
        "🛎️ فريق الدعم: +966501205651"
    )


def form_token(stay, user):
    return signing.dumps(
        {"stay": str(stay.pk), "user": user.pk, "id": str(uuid.uuid4())}, salt=SALT
    )


def read_form_token(value, stay, user):
    try:
        data = signing.loads(value, salt=SALT, max_age=86400)
        if data["stay"] != str(stay.pk) or data["user"] != user.pk:
            raise ValueError
        return uuid.UUID(data["id"])
    except (signing.BadSignature, ValueError, TypeError, KeyError) as exc:
        raise MorningStayError("انتهت صلاحية زر الإرسال؛ أعد فتح تفاصيل الحجز.") from exc


def dispatch(delivery_id):
    from .tasks import deliver_morning_payment_link

    try:
        deliver_morning_payment_link.delay(str(delivery_id))
    except Exception:
        # No provider call was made. Explicitly expose queue failure; no historical sweep.
        MorningPaymentLinkDelivery.objects.filter(pk=delivery_id, state="queued").update(
            state="failed", last_error="queue_unavailable"
        )


def queue_initial(stay):
    if not configured():
        return None
    identifier = uuid.uuid5(stay.pk, "initial-payment-link")
    delivery, created = MorningPaymentLinkDelivery.objects.get_or_create(
        pk=identifier, defaults={"stay": stay, "fingerprint": fingerprint(stay)}
    )
    if created:
        transaction.on_commit(lambda: dispatch(delivery.pk))
    return delivery


def resend(stay_id, request_id, *, client=None):
    current = MorningStay.objects.select_related("property").get(pk=stay_id)
    existing = MorningPaymentLinkDelivery.objects.filter(pk=request_id).first()
    if existing:
        if existing.stay_id != current.pk:
            raise MorningStayError("طلب الإرسال لا يخص هذا الحجز.")
        return existing
    require_unpaid(current)
    evidence = verify_window(
        current.property, current.starts_at, current.ends_at, guests=current.guests, client=client
    )
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=current.property_id)
        stay = MorningStay.objects.select_for_update().select_related("property").get(pk=stay_id)
        existing = MorningPaymentLinkDelivery.objects.filter(pk=request_id).first()
        if existing:
            return existing
        if fingerprint(stay) != fingerprint(current):
            raise MorningStayError("تغير الطلب أثناء فحص التوفر؛ افتح التفاصيل مجددًا.")
        require_unpaid(stay)
        if (
            local_inflight_conflict(stay.property, stay.starts_at, stay.ends_at)
            or active_stays(
                stay.property, stay.starts_at, stay.ends_at + timedelta(hours=3), exclude=stay.pk
            ).exists()
        ):
            raise MorningStayError("توجد إقامة صباحية أخرى متداخلة؛ لم يُجدد الرابط.")
        stay.status = "open"
        stay.expires_at = min(timezone.now() + timedelta(minutes=5), stay.starts_at)
        stay.availability_checked_at = timezone.now()
        stay.availability_evidence = evidence
        stay.save(
            update_fields=[
                "status",
                "expires_at",
                "availability_checked_at",
                "availability_evidence",
                "updated_at",
            ]
        )
        delivery = MorningPaymentLinkDelivery.objects.create(
            pk=request_id, stay=stay, fingerprint=fingerprint(stay)
        )
        transaction.on_commit(lambda: dispatch(delivery.pk))
        return delivery


def send_link(delivery_id):
    delivery = MorningPaymentLinkDelivery.objects.select_related("stay__property").get(
        pk=delivery_id
    )
    if delivery.state != "queued":
        return delivery.state
    stay = delivery.stay
    try:
        require_unpaid(stay)
        if stay.expires_at <= timezone.now() or fingerprint(stay) != delivery.fingerprint:
            raise MorningStayError("stale")
        verify_window(stay.property, stay.starts_at, stay.ends_at, guests=stay.guests)
    except Exception:
        MorningPaymentLinkDelivery.objects.filter(pk=delivery_id, state="queued").update(
            state="expired", last_error="eligibility_not_verified"
        )
        return "expired"
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=stay.property_id)
        stay = MorningStay.objects.select_for_update().select_related("property").get(pk=stay.pk)
        delivery = MorningPaymentLinkDelivery.objects.select_for_update().get(pk=delivery_id)
        if delivery.state != "queued":
            return delivery.state
        try:
            require_unpaid(stay)
            if (
                stay.expires_at <= timezone.now()
                or fingerprint(stay) != delivery.fingerprint
                or local_inflight_conflict(stay.property, stay.starts_at, stay.ends_at)
                or active_stays(
                    stay.property,
                    stay.starts_at,
                    stay.ends_at + timedelta(hours=3),
                    exclude=stay.pk,
                ).exists()
            ):
                raise MorningStayError("stale")
        except MorningStayError:
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
                recipient=recipient, body=body, reference_id="morning-pay-" + str(delivery.pk)
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
    MorningPaymentLinkDelivery.objects.filter(pk=delivery_id, state="sending").update(
        state=state, last_error=error, provider_id=provider_id, completed_at=timezone.now()
    )
    return state
