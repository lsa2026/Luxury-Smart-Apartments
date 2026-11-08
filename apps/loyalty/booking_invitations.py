"""Owner-requested enrollment invitations with shared cross-channel deduplication."""

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

from . import invitation_history as history
from . import invitation_registry as registry
from .launch import booking_allowed
from .models import JoinInvitation
from .past_guest_invitation_copy import invitation_caption, invitation_image
from .past_guest_offers import KEY as PAST_OFFER_KEY
from .past_guest_offers import apply_for_booking, issue, quote, retain_sent_offer
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
    return digest(
        {
            key: target[key]
            for key in (recipient, "phone", "language", "url", "guest_name", "reward")
        }
    )


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
    reward = quote(booking)
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
        raise ValidationError("يلزم رقم جوال دولي صحيح محفوظ في الحجز لاستلام المكافأة.") from exc
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
    target = {
        "phone": phone,
        "reward": reward,
        "guest_name": f"{intent.guest_first_name} {intent.guest_last_name}".strip(),
        "language": language,
        "url": base + reverse("accounts:rewards"),
        "customer": intent.customer_id,
        "email": email if channel == "email" else intent.guest_email,
    }
    invitation_caption(target["language"], target["url"], target["guest_name"], target["reward"])
    return target


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
            "preview_text": invitation_caption(
                target["language"], target["url"], target["guest_name"], target["reward"]
            ),
            "preview_image": "data:image/png;base64,"
            + b64encode(invitation_image(target["language"])).decode("ascii"),
        }
    return {
        **info,
        **preview,
        "language_code": target["language"],
        "manual_resend": bool(history.receipts(booking)),
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
                "history": history.fingerprint(booking),
                "manual_resend": bool(history.receipts(booking)),
            },
            salt=SALT,
        ),
    }


def invitation_context(user, booking):
    if not is_operations_owner(user):
        return None
    whatsapp = _channel_context(user, booking, "whatsapp")
    return {
        **whatsapp,
        "history": history.receipts(booking),
        "channels": [whatsapp, _channel_context(user, booking, "email")],
    }


def _manual_repeat_claim(booking, target, request_id, channel):
    """Explicit owner-only repeat; the n8n suppression receipt is never reset."""
    if registry.member_exists(target["phone"], target["email"], target["customer"]):
        raise ValidationError("الضيف عضو بالفعل؛ لا حاجة إلى دعوة انضمام جديدة.")
    shared = JoinInvitation.objects.filter(reservation_key=registry.booking_key(booking)).first()
    if shared:
        if shared.state == "claimed" and shared.expires_at > timezone.now():
            raise ValidationError("توجد دعوة قيد التحضير؛ انتظر نتيجتها قبل إعادة الإرسال.")
        return shared, False
    # Older manual receipts predate the shared registry. This new suppression
    # row covers the explicit request without fabricating a prior provider result.
    shared = JoinInvitation.objects.create(
        reservation_key=registry.booking_key(booking),
        event_key=registry.keyed("website:manual-repeat:" + request_id),
        phone=target["phone"],
        email=target["email"],
        phone_digest=phone_hash(target["phone"]),
        email_digest=recipient_hmac(target["email"]) if target["email"] else "",
        customer_id=target["customer"],
        language=target["language"],
        source="website-manual",
        origin="website",
        channel=channel,
        expires_at=timezone.now() + timedelta(minutes=15),
    )
    return shared, True


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
        if proof.get("history") != history.fingerprint(booking):
            raise ValidationError("تحدّث سجل الدعوات؛ افتح الحجز وراجعه قبل الإرسال.")
        manual_resend = bool(proof.get("manual_resend"))
        if manual_resend and request.POST.get("confirm_resend") != "on":
            raise ValidationError("راجع سجل الدعوات وأكّد رغبتك في إعادة الإرسال.")
        if history.pending(booking):
            raise ValidationError("يوجد طلب قيد الإرسال؛ انتظر نتيجته قبل إنشاء طلب آخر.")
        target = _target(booking, channel)
        if proof.get("target") != _target_digest(target, channel):
            raise ValidationError("تغيرت بيانات المستلم؛ افتح الحجز وراجع الدعوة مجددًا.")
        if manual_resend:
            shared, record_shared = _manual_repeat_claim(booking, target, request_id, channel)
            decision = "eligible"
        else:
            record_shared = True
            decision, shared = registry.claim(
                key=registry.booking_key(booking),
                phone=target["phone"],
                email=target["email"],
                customer_id=target["customer"],
                language=target["language"],
                event_id=request_id,
                source="website-manual",
                origin="website",
                channel=channel,
            )
        if decision != "eligible":
            raise ValidationError(
                "الضيف عضو أو توجد دعوة سابقة/محجوزة؛ راجع السجل المشترك قبل الإرسال."
            )
        recipient_digest = (
            phone_hash(target["phone"])
            if channel == "whatsapp"
            else recipient_hmac(target["email"])
        )
        recipient_key = "phone_digest" if channel == "whatsapp" else "recipient_digest"
        if not (manual_resend and retain_sent_offer(booking, target)):
            issue(booking, target, request_id, channel, request.user)
        row = {
            "manual_resend": manual_resend,
            "record_shared": record_shared,
            "provider_reference": shared.provider_reference
            + (":manual:" + request_id if manual_resend else ""),
            "past_guest_offer": PAST_OFFER_KEY,
            "shared_invitation_id": str(shared.pk),
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
            summary="طلب دعوة خاصة بإقامة سابقة مؤهلة مع عرض مكافأة ٣٪ بعد الانضمام وتوثيق الجوال",
            metadata={
                "language": target["language"],
                "channel": channel,
                "request_id": request_id or "",
                "authorization": "owner_manual",
                "past_stay_reward": target["reward"],
                "manual_resend": manual_resend,
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


def _finish(
    booking_id, status, *, expected="sending", channel="whatsapp", request_id=None, provider_id=""
):
    # Bookkeeping must work even if the programme is switched off during delivery.
    with registry.invitation_lock():
        booking = Reservation.objects.select_for_update().get(pk=booking_id)
        row = _request_row(booking, channel, request_id)
        if row.get("status") != expected:
            return
        row.update(status=status, updated_at=timezone.now().isoformat(), provider_id=provider_id)
        if status == "accepted":
            row["accepted_at"] = row["updated_at"]
        if row.get("shared_invitation_id") and row.get("record_shared", True):
            shared = JoinInvitation.objects.select_for_update().get(pk=row["shared_invitation_id"])
            registry.record(shared, "unknown" if status == "uncertain" else status, provider_id)
        booking.save(update_fields=["loyalty_state", "updated_at"])
        if row.get("past_guest_offer") == PAST_OFFER_KEY and status in {"accepted", "uncertain"}:
            transaction.on_commit(lambda: apply_for_booking(booking.pk), robust=True)


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


def invitation_copy(language, guest_name="", reward=None):
    url = settings.SITE_BASE_URL.rstrip("/") + reverse("accounts:rewards")
    return invitation_caption(language, url, guest_name, reward).removesuffix("\n\n" + url)


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
            "message": invitation_copy(
                target["language"], target.get("guest_name", ""), target["reward"]
            ),
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
    with registry.invitation_lock():
        booking = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("booking_intent__customer", "property")
            .get(pk=booking_id)
        )
        row = _request_row(booking, channel, request_id)
        if row.get("status") != "queued":
            return
        try:
            if row.get("past_guest_offer") != PAST_OFFER_KEY:
                raise ValidationError("Legacy invitation needs a fresh owner review.")
            operator = get_user_model().objects.filter(pk=row.get("operator")).first()
            if not is_operations_owner(operator):
                raise ValidationError("Invitation operator no longer authorized.")
            target = _target(booking, channel)
            target_digest = _target_digest(target, channel) if request_id else digest(target)
            if target_digest != row["target_digest"] or timezone.now() - datetime.fromisoformat(
                row["requested_at"]
            ) > timedelta(minutes=15):
                raise ValidationError("Invitation changed or expired.")
            exclude = (str(booking.pk), channel, request_id or "")
            if row.get("shared_invitation_id"):
                shared = JoinInvitation.objects.select_for_update().get(
                    pk=row["shared_invitation_id"]
                )
            else:
                decision, shared = registry.claim(
                    key=registry.booking_key(booking),
                    phone=target["phone"],
                    email=target["email"],
                    customer_id=target["customer"],
                    language=target["language"],
                    event_id=request_id or str(booking.pk),
                    source="website-manual",
                    origin="website",
                    channel=channel,
                    exclude_legacy=exclude,
                )
                if decision != "eligible":
                    raise ValidationError("Existing invitation or membership.")
                row["shared_invitation_id"] = str(shared.pk)
            if channel == "whatsapp":
                content = invitation_image(target["language"])
                caption = invitation_caption(
                    target["language"], target["url"], target["guest_name"], target["reward"]
                )
            else:
                email_request = invitation_email_request(target)
            if row.get("manual_resend"):
                if history.pending(booking, exclude_id=request_id) or registry.member_exists(
                    target["phone"], target["email"], target["customer"]
                ):
                    raise ValidationError("Another send or membership exists.")
                retain_sent_offer(booking, target)
                if row.get("record_shared"):
                    if shared.state != "claimed" or shared.expires_at <= timezone.now():
                        raise ValidationError("Repeat claim expired or consumed.")
                    shared.state = "unknown"
                    shared.dispatched_at = timezone.now()
                    shared.save(update_fields=["state", "dispatched_at", "updated_at"])
            elif (
                registry.consume(shared, phone=target["phone"], exclude_legacy=exclude)
                != "eligible"
            ):
                raise ValidationError("Existing invitation or membership.")
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
                    reference_id=row.get("provider_reference") or shared.provider_reference,
                )
            if not isinstance(result, dict) or not result.get("id"):
                raise ValueError("Unconfirmed provider outcome.")
        elif not DjangoEmailProvider().send(email_request).sent:
            raise ValueError("Unconfirmed email outcome.")
    except Exception:
        _finish(booking_id, "uncertain", channel=channel, request_id=request_id)
        return
    _finish(
        booking_id,
        "accepted",
        channel=channel,
        request_id=request_id,
        provider_id=str(result["id"])[:100] if channel == "whatsapp" else "",
    )
