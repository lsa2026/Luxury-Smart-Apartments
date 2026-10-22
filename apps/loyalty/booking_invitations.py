"""Explicit owner invitations from one completed booking; never a history campaign."""

from datetime import datetime, timedelta
from urllib.parse import urlsplit

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.access import is_operations_owner, require_operations_owner
from apps.accounts.models import VerifiedPhone
from apps.accounts.phone_messages import phone_logo, supported_language
from apps.core.phone_numbers import normalize_phone_number
from apps.notifications.models import AuditLog
from apps.notifications.services.ultramsg import UltraMsgClient
from apps.notifications.whatsapp_style import BRAND
from apps.reservations.models import Reservation
from apps.reservations.services.stay_lifecycle import checkout_datetime

from .launch import booking_allowed
from .models import Member
from .phone_gifts import delivery_ready, phone_hash, recipient_language
from .services import digest, posting_transaction

STATE = "owner_join_invitation"
SALT = "loyalty-booking-join-invitation-v1"
STATUSES = {
    "queued": "بانتظار إرسال دعوة الولاء",
    "sending": "جارٍ طلب الإرسال؛ لا تكرر الدعوة",
    "accepted": "قبِل مزود واتساب الدعوة؛ الوصول والقراءة غير مؤكدين",
    "uncertain": "نتيجة الإرسال غير مؤكدة؛ راجعها قبل أي محاولة أخرى",
    "queue_failed": "تعذر جدولة الإرسال؛ لم تُرسل الدعوة",
    "blocked": "لم تُرسل الدعوة بسبب تغير شروط الإرسال",
}


def _target(booking):
    """Membership invitation only: no earned-stay, arrival or payment assertion."""
    from .views import approved_terms

    if not (
        settings.LOYALTY_ENABLED
        and settings.LOYALTY_UI_ENABLED
        and approved_terms()
        and not settings.LOYALTY_UI_PREVIEW
        and booking_allowed(booking)
        and delivery_ready()
    ):
        raise ValidationError("دعوات الولاء غير متاحة ضمن إعدادات البرنامج والإرسال الحالية.")
    intent = booking.booking_intent
    if not intent:
        raise ValidationError("لا تتوفر بيانات ضيف مرتبطة بهذا الحجز.")
    if booking.normalized_status not in Reservation.ACTIVE_STATUSES:
        raise ValidationError("حالة الحجز لا تسمح بدعوة ضيف إقامة مكتملة.")
    if timezone.now() < checkout_datetime(booking):
        raise ValidationError("تتاح هذه الدعوة للضيوف السابقين بعد انتهاء فترة الإقامة.")
    if not intent.marketing_consent:
        raise ValidationError("لا توجد موافقة محفوظة على الرسائل التسويقية؛ لم تُرسل دعوة.")
    try:
        phone = normalize_phone_number(intent.guest_phone)
    except ValueError as exc:
        raise ValidationError("يلزم رقم جوال دولي صحيح محفوظ في الحجز.") from exc
    binding = VerifiedPhone.objects.select_related("user").filter(phone=phone).first()
    customer = intent.customer if intent.customer_id else None
    if (customer and not customer.is_active) or (binding and not binding.user.is_active):
        raise ValidationError("حساب الضيف غير نشط؛ لا تتاح دعوة الانضمام.")
    identities = Q(customer_id=intent.customer_id) if intent.customer_id else Q(pk__in=[])
    if binding:
        identities |= Q(customer_id=binding.user_id)
    if intent.guest_email:
        identities |= Q(customer__email__iexact=intent.guest_email.strip())
    if Member.objects.filter(identities).exists():
        raise ValidationError("للضيف سجل عضوية قائم؛ راجع حالته بدل إرسال دعوة انضمام جديدة.")
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
        "email": intent.guest_email,
    }


def invitation_context(user, booking):
    if not is_operations_owner(user):
        return None
    row = booking.loyalty_state.get(STATE, {})
    status = row.get("status", "")
    if status:
        at = datetime.fromisoformat(row["requested_at"])
        if status in {"queued", "sending"} and timezone.now() - at > timedelta(minutes=15):
            status = "uncertain"
        return {"status": STATUSES.get(status, STATUSES["uncertain"])}
    try:
        target = _target(booking)
    except ValidationError as exc:
        return {"reason": "؛ ".join(exc.messages)}
    return {
        "phone": target["phone"],
        "language": {"ar": "العربية", "en": "الإنجليزية", "fr": "الفرنسية"}[target["language"]],
        "confirmation": signing.dumps(
            {"booking": str(booking.pk), "operator": user.pk, "target": digest(target)}, salt=SALT
        ),
    }


def queue_booking_invitation(request, booking_id):
    require_operations_owner(request.user)
    if request.POST.get("confirm_loyalty_invitation") != "on":
        raise ValidationError("أكد مراجعة رقم الضيف والموافقة المحفوظة قبل إرسال الدعوة.")
    try:
        proof = signing.loads(request.POST.get("confirmation", ""), salt=SALT, max_age=600)
    except signing.BadSignature as exc:
        raise ValidationError("انتهت صلاحية المراجعة؛ افتح الحجز وراجع الدعوة مجددًا.") from exc
    if proof.get("booking") != str(booking_id) or proof.get("operator") != request.user.pk:
        raise ValidationError("مراجعة الدعوة لا تطابق الحجز أو المستخدم.")
    with posting_transaction():
        booking = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("booking_intent__customer", "property")
            .get(pk=booking_id)
        )
        if booking.loyalty_state.get(STATE):
            return "سبق طلب دعوة لهذا الحجز؛ لم يُكرر الإرسال."
        target = _target(booking)
        if proof.get("target") != digest(target):
            raise ValidationError("تغيرت بيانات المستلم؛ افتح الحجز وراجع الدعوة مجددًا.")
        recipient_digest = phone_hash(target["phone"])
        # Serialize different booking pages for the same phone under the posting lock.
        if Reservation.objects.filter(
            **{f"loyalty_state__{STATE}__phone_digest": recipient_digest}
        ).exists():
            raise ValidationError("سبق طلب دعوة لهذا الرقم من حجز آخر؛ راجع نتيجة الإرسال.")
        booking.loyalty_state[STATE] = {
            "status": "queued",
            "requested_at": timezone.now().isoformat(),
            "operator": request.user.pk,
            "target_digest": digest(target),
            "phone_digest": recipient_digest,
            "language": target["language"],
        }
        booking.save(update_fields=["loyalty_state", "updated_at"])
        AuditLog.objects.create(
            actor_user=request.user,
            action="loyalty.booking_invitation_requested",
            object_type="Reservation",
            object_reference=str(booking.pk),
            summary="طلب دعوة انضمام اختيارية من الحجز دون عضوية أو نقاط أو ربط داعٍ",
            metadata={"language": target["language"]},
        )
        transaction.on_commit(lambda: _dispatch(str(booking.pk)), robust=True)
    return "حُفظ طلب دعوة الولاء؛ راجع حالة الإرسال في بيانات الضيف."


def _finish(booking_id, status, *, expected="sending"):
    # Bookkeeping must work even if the programme is switched off during delivery.
    with transaction.atomic():
        booking = Reservation.objects.select_for_update().get(pk=booking_id)
        row = booking.loyalty_state.get(STATE, {})
        if row.get("status") != expected:
            return
        row.update(status=status, updated_at=timezone.now().isoformat())
        booking.save(update_fields=["loyalty_state", "updated_at"])


def _dispatch(booking_id):
    try:
        deliver_booking_invitation.delay(booking_id)
    except Exception:
        _finish(booking_id, "queue_failed", expected="queued")


def invitation_caption(language, url):
    text = {
        "ar": (
            "يسعدنا دعوتك للانضمام إلى برنامج مكافآت ضيوفنا.\n"
            "أنشئ حسابك أو سجّل الدخول، واقرأ الشروط ثم انضم بموافقتك. "
            "الانضمام مجاني واختياري، ولا تُضاف نقاط بأثر رجعي لإقامتك السابقة. "
            "هذه دعوة فقط؛ لم تُفعّل عضوية ولم تُحوّل نقاط."
        ),
        "en": (
            "You are invited to join our guest rewards programme.\n"
            "Create an account or sign in, read the terms and choose to join. "
            "Membership is free and optional. Your previous stay does not earn retrospective "
            "points. This invitation creates no membership and transfers no points."
        ),
        "fr": (
            "Nous vous invitons à rejoindre notre programme de fidélité.\n"
            "Créez un compte ou connectez-vous, lisez les conditions et choisissez d’adhérer. "
            "L’adhésion est gratuite et facultative. Votre séjour précédent ne donne pas de "
            "points rétroactifs. Cette invitation ne crée aucune adhésion ni transfert de points."
        ),
    }[language]
    return f"✨ *{BRAND}*\n\n{text}\n\n{url}"


@shared_task(ignore_result=True, soft_time_limit=35, time_limit=45)
def deliver_booking_invitation(booking_id):
    with transaction.atomic():
        booking = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("booking_intent__customer", "property")
            .get(pk=booking_id)
        )
        row = booking.loyalty_state.get(STATE, {})
        if row.get("status") != "queued":
            return
        try:
            operator = get_user_model().objects.filter(pk=row.get("operator")).first()
            if not is_operations_owner(operator):
                raise ValidationError("Invitation operator no longer authorized.")
            target = _target(booking)
            if digest(target) != row["target_digest"] or timezone.now() - datetime.fromisoformat(
                row["requested_at"]
            ) > timedelta(minutes=15):
                raise ValidationError("Invitation changed or expired.")
            content = phone_logo()
            caption = invitation_caption(target["language"], target["url"])
        except (ValidationError, OSError, KeyError, ValueError):
            row["status"] = "blocked"
            booking.save(update_fields=["loyalty_state", "updated_at"])
            return
        row["status"] = "sending"
        booking.save(update_fields=["loyalty_state", "updated_at"])
    try:
        with UltraMsgClient() as provider:
            result = provider.send_image(
                recipient=target["phone"],
                content=content,
                caption=caption,
                reference_id=f"lsa-booking-invite:{booking_id}",
            )
        if not isinstance(result, dict) or not result.get("id"):
            raise ValueError("Unconfirmed provider outcome.")
    except Exception:
        _finish(booking_id, "uncertain")
        return
    _finish(booking_id, "accepted")
