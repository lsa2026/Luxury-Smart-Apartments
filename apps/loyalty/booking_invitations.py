"""Owner-requested invitations; each manual click is an independent request."""

from base64 import b64encode
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.access import is_operations_owner, require_operations_owner
from apps.accounts.phone_messages import supported_language
from apps.core.phone_numbers import normalize_phone_number
from apps.notifications.models import AuditLog
from apps.notifications.services.email import (
    DjangoEmailProvider,
    EmailMessageRequest,
    build_guest_email,
    recipient_hmac,
)
from apps.notifications.services.ultramsg import UltraMsgClient
from apps.reservations.models import Reservation

from .invitation_design import invitation_image
from .launch import booking_allowed
from .phone_gifts import delivery_ready, phone_hash, recipient_language
from .services import digest, posting_transaction

STATE = "owner_join_invitation"
REQUESTS = "owner_join_invitation_requests"
LATEST = "owner_join_invitation_latest"
EMAIL_STATE = "owner_join_invitation_email"
EMAIL_REQUESTS = "owner_join_invitation_email_requests"
EMAIL_LATEST = "owner_join_invitation_email_latest"
CHANNELS = {"whatsapp": "واتساب", "email": "البريد الإلكتروني"}
SALT = "loyalty-booking-join-invitation-v1"
STATUSES = {
    "queued": "بانتظار إرسال دعوة الولاء",
    "sending": "جارٍ طلب الإرسال؛ لا تكرر الدعوة",
    "accepted": "قبِل مزود واتساب الدعوة؛ الوصول والقراءة غير مؤكدين",
    "uncertain": "نتيجة الإرسال غير مؤكدة؛ راجعها قبل أي محاولة أخرى",
    "queue_failed": "تعذر جدولة الإرسال؛ لم تُرسل الدعوة",
    "blocked": "لم تُرسل الدعوة بسبب تغير شروط الإرسال",
}


def _state(channel):
    if channel not in CHANNELS:
        raise ValidationError("اختر واتساب أو البريد الإلكتروني لإرسال الدعوة.")
    return STATE if channel == "whatsapp" else EMAIL_STATE


def _target_digest(target, channel):
    recipient = "phone" if channel == "whatsapp" else "email"
    return digest({key: target[key] for key in (recipient, "language", "url")})


def _request_keys(channel):
    _state(channel)
    return (REQUESTS, LATEST) if channel == "whatsapp" else (EMAIL_REQUESTS, EMAIL_LATEST)


def _target(booking, channel="whatsapp"):
    """Membership invitation only: no earned-stay, arrival or payment assertion."""
    from .views import approved_terms

    if not (
        settings.LOYALTY_ENABLED
        and settings.LOYALTY_UI_ENABLED
        and approved_terms()
        and not settings.LOYALTY_UI_PREVIEW
        and booking_allowed(booking)
    ):
        raise ValidationError("دعوات الولاء غير متاحة ضمن إعدادات البرنامج والإرسال الحالية.")
    intent = booking.booking_intent
    if not intent:
        raise ValidationError("لا تتوفر بيانات ضيف مرتبطة بهذا الحجز.")
    _state(channel)
    email = intent.guest_email.strip()
    if channel == "whatsapp" and not delivery_ready():
        raise ValidationError("إرسال واتساب غير متاح ضمن إعدادات الإرسال الحالية.")
    if channel == "email":
        if (
            not settings.EMAIL_DELIVERY_ENABLED
            or not settings.DEFAULT_FROM_EMAIL
            or any(
                backend in settings.EMAIL_BACKEND
                for backend in (".console.", ".dummy.", ".filebased.")
            )
        ):
            raise ValidationError("إرسال البريد الإلكتروني غير متاح ضمن الإعدادات الحالية.")
        try:
            validate_email(email)
        except ValidationError as exc:
            raise ValidationError("يلزم بريد إلكتروني صحيح محفوظ في الحجز.") from exc
    try:
        phone = normalize_phone_number(intent.guest_phone)
    except ValueError as exc:
        if channel == "whatsapp":
            raise ValidationError("يلزم رقم جوال دولي صحيح محفوظ في الحجز.") from exc
        phone = ""
    language = recipient_language(phone) or supported_language(intent.language)
    if not language:
        raise ValidationError("حدد لغة الضيف في الحجز قبل إرسال الدعوة.")
    base = settings.SITE_BASE_URL.rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("رابط برنامج الولاء الآمن غير متاح.")
    return {
        "phone": phone,
        "language": language,
        "url": base + reverse("accounts:rewards"),
        "customer": intent.customer_id,
        "email": email if channel == "email" else intent.guest_email,
    }


def _channel_context(user, booking, channel):
    info = {"channel": channel, "label": CHANNELS[channel]}
    requests_key, latest_key = _request_keys(channel)
    row = booking.loyalty_state.get(requests_key, {}).get(
        booking.loyalty_state.get(latest_key), {}
    ) or booking.loyalty_state.get(_state(channel), {})
    status = row.get("status", "")
    if status:
        at = datetime.fromisoformat(row["requested_at"])
        if status in {"queued", "sending"} and timezone.now() - at > timedelta(minutes=15):
            status = "uncertain"
        status_text = STATUSES.get(status, STATUSES["uncertain"]).replace(
            "واتساب", CHANNELS[channel]
        )
        if status == "sending":
            status_text = "آخر طلب قيد الإرسال."
        elif status == "uncertain":
            status_text = "نتيجة آخر طلب غير مؤكدة."
        info["last_status"] = status_text
    try:
        target = _target(booking, channel)
    except ValidationError as exc:
        return {**info, "reason": "؛ ".join(exc.messages)}
    if channel == "email":
        message = build_guest_email(invitation_email_request(target))
        preview = {"preview_subject": message.subject, "preview_text": message.body}
    else:
        preview = {
            "preview_text": invitation_caption(target["language"], target["url"]),
            "preview_image": "data:image/png;base64,"
            + b64encode(invitation_image(target["language"])).decode("ascii"),
        }
    return {
        **info,
        **preview,
        "language_code": target["language"],
        "recipient": target["phone"] if channel == "whatsapp" else target["email"],
        "phone": target["phone"],
        "language": {"ar": "العربية", "en": "الإنجليزية", "fr": "الفرنسية"}[target["language"]],
        "confirmation": signing.dumps(
            {
                "booking": str(booking.pk),
                "operator": user.pk,
                "channel": channel,
                "request_id": str(uuid4()),
                "target": _target_digest(target, channel),
            },
            salt=SALT,
        ),
    }


def invitation_context(user, booking):
    if not is_operations_owner(user):
        return None
    whatsapp = _channel_context(user, booking, "whatsapp")
    return {**whatsapp, "channels": [whatsapp, _channel_context(user, booking, "email")]}


def queue_booking_invitation(request, booking_id):
    require_operations_owner(request.user)
    channel = request.POST.get("channel", "whatsapp")
    requests_key, latest_key = _request_keys(channel)
    try:
        proof = signing.loads(request.POST.get("confirmation", ""), salt=SALT, max_age=600)
    except signing.BadSignature as exc:
        raise ValidationError("انتهت صلاحية المراجعة؛ افتح الحجز وراجع الدعوة مجددًا.") from exc
    if proof.get("booking") != str(booking_id) or proof.get("operator") != request.user.pk:
        raise ValidationError("مراجعة الدعوة لا تطابق الحجز أو المستخدم.")
    if proof.get("channel", "whatsapp") != channel:
        raise ValidationError("مراجعة الدعوة لا تطابق قناة الإرسال المختارة.")
    request_id = proof.get("request_id")
    if not request_id:
        raise ValidationError("حدّث صفحة الحجز لإرسال دعوة جديدة بقرارك.")
    with posting_transaction():
        booking = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("booking_intent__customer", "property")
            .get(pk=booking_id)
        )
        if request_id in booking.loyalty_state.get(requests_key, {}):
            return "حُفظ طلب هذه الضغطة مسبقًا. يمكنك إرسال دعوة جديدة من صفحة الحجز."
        target = _target(booking, channel)
        if proof.get("target") != _target_digest(target, channel):
            raise ValidationError("تغيرت بيانات المستلم؛ افتح الحجز وراجع الدعوة مجددًا.")
        recipient_digest = (
            phone_hash(target["phone"])
            if channel == "whatsapp"
            else recipient_hmac(target["email"])
        )
        recipient_key = "phone_digest" if channel == "whatsapp" else "recipient_digest"
        row = {
            "status": "queued",
            "requested_at": timezone.now().isoformat(),
            "operator": request.user.pk,
            "target_digest": _target_digest(target, channel),
            recipient_key: recipient_digest,
            "channel": channel,
            "language": target["language"],
        }
        booking.loyalty_state.setdefault(requests_key, {})[request_id] = row
        booking.loyalty_state[latest_key] = request_id
        booking.save(update_fields=["loyalty_state", "updated_at"])
        AuditLog.objects.create(
            actor_user=request.user,
            action="loyalty.booking_invitation_requested",
            object_type="Reservation",
            object_reference=str(booking.pk),
            summary="طلب دعوة انضمام اختيارية من الحجز دون عضوية أو نقاط أو ربط داعٍ",
            metadata={
                "language": target["language"],
                "channel": channel,
                "request_id": request_id or "",
                "authorization": "owner_manual",
            },
        )
        transaction.on_commit(
            lambda: _dispatch(str(booking.pk), channel, request_id),
            robust=True,
        )
    return "حُفظ طلب دعوة الولاء؛ راجع حالة الإرسال في قسم الدعوة."


def _request_row(booking, channel, request_id=None):
    if request_id:
        requests_key, _ = _request_keys(channel)
        return booking.loyalty_state.get(requests_key, {}).get(request_id, {})
    return booking.loyalty_state.get(_state(channel), {})


def _finish(booking_id, status, *, expected="sending", channel="whatsapp", request_id=None):
    # Bookkeeping must work even if the programme is switched off during delivery.
    with transaction.atomic():
        booking = Reservation.objects.select_for_update().get(pk=booking_id)
        row = _request_row(booking, channel, request_id)
        if row.get("status") != expected:
            return
        row.update(status=status, updated_at=timezone.now().isoformat())
        booking.save(update_fields=["loyalty_state", "updated_at"])


def _dispatch(booking_id, channel="whatsapp", request_id=None):
    try:
        if request_id:
            deliver_booking_invitation.delay(booking_id, channel, request_id)
        elif channel == "whatsapp":
            deliver_booking_invitation.delay(booking_id)
        else:
            deliver_booking_invitation.delay(booking_id, channel)
    except Exception:
        _finish(
            booking_id, "queue_failed", expected="queued", channel=channel, request_id=request_id
        )


def invitation_copy(language):
    return {
        "ar": (
            "أهلًا بك 🌿\n\n"
            "إقامتك معنا بداية لتجارب أجمل. يسعدنا دعوتك إلى برنامج الولاء لدى Luxury Living، "
            "لتستمتع بمكافآت إقاماتك القادمة وتتبادل هدايا النقاط مع أحبائك.\n\n"
            "الانضمام بخطوات بسيطة:\n"
            "١. أنشئ حسابك أو سجّل الدخول.\n"
            "٢. اقرأ الشروط، ثم انضم بموافقتك ووثّق جوالك.\n\n"
            "الانضمام مجاني واختياري. تُحتسب المكافآت وفق شروط البرنامج، "
            "ولا تُضاف نقاط بأثر رجعي. هذه دعوة فقط؛ لا تُفعّل عضوية ولا تُحوّل نقاط تلقائيًا.\n\n"
            "نسعد بعودتك إلى Luxury Living 💚"
        ),
        "en": (
            "Welcome 🌿\n\n"
            "Let your next stay bring more to enjoy. Join the Luxury Living rewards programme "
            "to earn rewards on future stays and share gifts of points with loved ones.\n\n"
            "Getting started is simple:\n"
            "1. Create an account or sign in.\n"
            "2. Read the terms, choose to join and verify your mobile.\n\n"
            "Membership is free and optional. Rewards follow the programme terms; "
            "previous stays do not earn retrospective points. This invitation does not "
            "activate membership or transfer points automatically.\n\n"
            "We look forward to welcoming you back to Luxury Living 💚"
        ),
        "fr": (
            "Bienvenue 🌿\n\n"
            "Prolongez le plaisir de vos séjours avec le programme de fidélité Luxury Living : "
            "des récompenses pour vos prochains séjours et des points à offrir à vos proches.\n\n"
            "Pour nous rejoindre :\n"
            "1. Créez un compte ou connectez-vous.\n"
            "2. Lisez les conditions, choisissez d’adhérer et vérifiez votre mobile.\n\n"
            "L’adhésion est gratuite et facultative. Les récompenses suivent les conditions "
            "du programme, sans points rétroactifs. Cette invitation n’active aucune adhésion "
            "et ne transfère pas de points automatiquement.\n\n"
            "Au plaisir de vous retrouver chez Luxury Living 💚"
        ),
    }[language]


def invitation_caption(language, url):
    title, action = {
        "ar": ("دعوة خاصة إلى مكافآت ضيوفنا", "ابدأ رحلتك مع المكافآت"),
        "en": ("A personal invitation to guest rewards", "Start your rewards journey"),
        "fr": ("Une invitation à nos avantages", "Découvrez vos récompenses"),
    }[language]
    caption = f"🎁 *{title}*\n\n{invitation_copy(language)}\n\n*{action}:*\n{url}"
    if len(caption.encode("utf-16-le")) // 2 > 1024:
        raise ValidationError("Invitation caption exceeds the provider limit.")
    return caption


def invitation_email_request(target):
    """One message definition for the read-only preview and actual delivery."""
    heading = {
        "ar": "دعوة للانضمام إلى برنامج الولاء",
        "en": "Invitation to join our rewards programme",
        "fr": "Invitation à rejoindre notre programme de fidélité",
    }[target["language"]]
    return EmailMessageRequest(
        recipient=target["email"],
        subject=heading,
        template_name="loyalty",
        language=target["language"],
        context={
            "heading": heading,
            "message": invitation_copy(target["language"]),
            "action_url": target["url"],
            "invitation": True,
            "action_label": {
                "ar": "الانضمام إلى برنامج الولاء",
                "en": "Join the rewards programme",
                "fr": "Rejoindre le programme de fidélité",
            }[target["language"]],
        },
    )


@shared_task(ignore_result=True, soft_time_limit=35, time_limit=45)
def deliver_booking_invitation(booking_id, channel="whatsapp", request_id=None):
    _state(channel)
    with transaction.atomic():
        booking = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("booking_intent__customer", "property")
            .get(pk=booking_id)
        )
        row = _request_row(booking, channel, request_id)
        if row.get("status") != "queued":
            return
        try:
            operator = get_user_model().objects.filter(pk=row.get("operator")).first()
            if not is_operations_owner(operator):
                raise ValidationError("Invitation operator no longer authorized.")
            target = _target(booking, channel)
            target_digest = _target_digest(target, channel) if request_id else digest(target)
            if target_digest != row["target_digest"] or timezone.now() - datetime.fromisoformat(
                row["requested_at"]
            ) > timedelta(minutes=15):
                raise ValidationError("Invitation changed or expired.")
            if channel == "whatsapp":
                content = invitation_image(target["language"])
                caption = invitation_caption(target["language"], target["url"])
            else:
                email_request = invitation_email_request(target)
        except (ValidationError, OSError, KeyError, ValueError):
            row["status"] = "blocked"
            booking.save(update_fields=["loyalty_state", "updated_at"])
            return
        row["status"] = "sending"
        booking.save(update_fields=["loyalty_state", "updated_at"])
    try:
        if channel == "whatsapp":
            with UltraMsgClient() as provider:
                result = provider.send_image(
                    recipient=target["phone"],
                    content=content,
                    caption=caption,
                    reference_id=f"lsa-booking-invite:{request_id or booking_id}",
                )
            if not isinstance(result, dict) or not result.get("id"):
                raise ValueError("Unconfirmed provider outcome.")
        elif not DjangoEmailProvider().send(email_request).sent:
            raise ValueError("Unconfirmed email outcome.")
    except Exception:
        _finish(booking_id, "uncertain", channel=channel, request_id=request_id)
        return
    _finish(booking_id, "accepted", channel=channel, request_id=request_id)
