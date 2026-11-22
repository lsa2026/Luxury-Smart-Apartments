"""Booking-scoped proof and issuance; no Telegram sends or account credential writes."""

import re
import uuid
from datetime import timedelta

from django import forms
from django.db import transaction
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_variables

from apps.accounts.models import PhoneChallenge, VerifiedPhone
from apps.accounts.phone_auth import recent_primary
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_booking_phone
from apps.notifications.models import AuditLog

from . import telegram_authority as authority
from . import telegram_backend as backend
from .models import Reservation, TelegramActivation
from .security import SESSION_MARKER_KEY, session_key_hash
from .telegram_copy import STATES, UI, copy_for

LABELS = {
    "disabled": "تفعيل تليجرام غير متاح حاليًا.",
    "unverified": "يلزم توثيق أن رقم الجوال يخص صاحب هذا الحجز قبل تجهيز الرابط.",
    "missing": "لا يوجد رقم موثّق لهذا الحجز. تواصل مع الإدارة للتحقق عبر قناة الحجز.",
    "active": (
        "سبق تجهيز رابط صالح. لا نصدر رابطًا آخر تلقائيًا؛ استخدم النسخة التي ظهرت عند الإصدار."
    ),
    "expired": "انتهت صلاحية الرابط السابق. يمكنك طلب رابط جديد بعد إعادة التحقق.",
    "bound": "تم ربط تليجرام بهذا الحجز بالفعل.",
    "unavailable": "تعذر تأكيد حالة الربط. لا تعاود طلب الإصدار حتى تراجع الإدارة النتيجة.",
    "uncertain": "نتيجة الإصدار غير مؤكدة؛ أوقفنا التكرار وتحتاج الإدارة لمراجعتها.",
    "processing": "طلب التفعيل قيد المعالجة؛ لا تكرر الطلب.",
    "hold": "لم يصدر رابط. يلزم مراجعة توثيق الهاتف وحالة الحجز لدى الإدارة.",
    "issued": "رابط التفعيل جاهز. افتح البوت وشارك جهة اتصالك الذاتية لإكمال التحقق.",
    "closed": "التفعيل غير متاح لهذا الحجز.",
}


class AdminProofForm(forms.Form):
    phone = forms.CharField(
        label="رقم الضيف الدولي بعد التحقق",
        max_length=32,
        help_text=(
            "إذا كان الهاتف مفقودًا أو مختلفًا في هوستاوي، صححه هناك أولًا "
            "ثم أعد فتح الحجز لتحديث بياناته قبل التفعيل."
        ),
    )
    evidence_ref = forms.RegexField(
        label="مرجع إجراء التحقق الخاص بالحجز", regex=r"^[A-Za-z0-9:_-]{6,100}$", max_length=100
    )
    confirmed = forms.BooleanField(
        label="تحققت من أن الهاتف يخص صاحب هذا الحجز عبر قناة مستقلة؛ ليس مجرد رقم هوستاوي."
    )

    def clean_phone(self):
        try:
            return normalize_booking_phone(self.cleaned_data["phone"])
        except InvalidPhoneNumber as exc:
            raise forms.ValidationError("راجع رقم الهاتف الدولي.") from exc


def eligible(reservation):
    from .stay_extension_models import StayExtension

    return bool(
        reservation.hostaway_reservation_id
        and reservation.normalized_status in Reservation.ACTIVE_STATUSES
        and not reservation.is_test
        and not StayExtension.objects.filter(reservation=reservation).exists()
    )


def key_for(reservation):
    key = str(reservation.hostaway_reservation_id or "")

    return key if re.fullmatch(r"[1-9][0-9]{1,15}", key) else ""


def guest_proof(request, reservation):
    intent = reservation.booking_intent
    if (
        not intent
        or not request.user.is_authenticated
        or not request.user.is_active
        or intent.customer_id != request.user.pk
    ):
        return None
    binding = VerifiedPhone.objects.filter(user=request.user).first()
    if not binding or binding.phone != intent.guest_phone:
        return None
    # A completed mobile sign-in deliberately does not set PRIMARY_AUTH. Accept
    # its existing consumed, session-bound proof without changing the login flow.
    recent_mobile = bool(
        request.session.get(SESSION_MARKER_KEY)
        and PhoneChallenge.objects.filter(
            user=request.user,
            phone=binding.phone,
            binding_version=binding.version,
            purpose="login",
            status="consumed",
            code_digest="",
            session_digest=session_key_hash(request),
            created_at__gte=timezone.now() - timedelta(minutes=15),
            created_at__lte=timezone.now(),
        ).exists()
    )
    if not recent_primary(request) and not recent_mobile:
        return None
    return binding.phone, "website_verified", "verified-phone:" + str(binding.version)


def context(request, reservation, *, admin=False):
    from .telegram_workgroup import manual_mode as workgroup_enabled

    if workgroup_enabled():
        return None
    request._disable_google_integrations = True
    value = {"admin": admin, "state": "disabled", "can_issue": False, "form": AdminProofForm()}
    value["action_url"] = reverse(
        "notifications:booking_detail" if admin else "reservations:telegram_activation",
        kwargs={"reservation_id": reservation.pk}
        if admin
        else {"public_reference": reservation.public_reference},
    )
    if backend.configured():
        value["state"] = "closed"
        if eligible(reservation) and key_for(reservation):
            client = None
            try:
                client = backend.Backend()
                client.authority_context = authority.booking_scope(
                    request, reservation, admin=admin
                )
                value["state"] = client.status(key_for(reservation))
                previous = TelegramActivation.objects.filter(reservation=reservation).first()
                if value["state"] not in {"bound", "active"} and previous:
                    if previous.state in {"uncertain", "processing"}:
                        value["state"] = previous.state
                    elif (
                        previous.state == "issued"
                        and previous.expires_at
                        and previous.expires_at > timezone.now()
                    ):
                        value["state"] = "active"
                value["can_issue"] = value["state"] in {"unverified", "expired", "hold"} and (
                    admin or bool(guest_proof(request, reservation))
                )
                if not admin and not reservation.booking_intent.guest_phone:
                    value["state"], value["can_issue"] = "missing", False
            except (backend.Unavailable, authority.Denied):
                value["state"] = "unavailable"
            finally:
                if client:
                    client.close()
    language = copy_for(request)
    value["copy"] = UI[language]
    value["message"] = STATES.get(language, LABELS).get(value["state"], LABELS["hold"])
    return value


@sensitive_variables()
def issue(request, reservation, *, admin=False):
    """Caller has checked booking access and CSRF. Return URL only in this response."""
    result = {"state": "disabled", "url": ""}
    if not backend.configured():
        return result
    if not eligible(reservation) or not key_for(reservation):
        return {"state": "closed", "url": ""}
    if admin:
        form = AdminProofForm(request.POST)
        if not form.is_valid():
            return {"state": "unverified", "url": "", "form": form}
        proof = (form.cleaned_data["phone"], "admin_verified", form.cleaned_data["evidence_ref"])
    else:
        proof = guest_proof(request, reservation)
    if not proof:
        return {"state": "unverified", "url": ""}
    phone, kind, evidence = proof
    # Serialize the whole operation on the reservation. Unknown outcomes remain
    # durable holds after a crash, so another worker/browser cannot replay them.
    with transaction.atomic():
        current = Reservation.objects.select_for_update().get(pk=reservation.pk)
        if not eligible(current) or key_for(current) != key_for(reservation):
            return {"state": "closed", "url": ""}
        if not admin and guest_proof(request, current) != proof:
            return {"state": "unverified", "url": ""}
        row = TelegramActivation.objects.filter(reservation=current).first()
        if row and (
            row.state in {"processing", "uncertain", "bound"}
            or row.state == "issued"
            and row.expires_at > timezone.now()
        ):
            return {"state": "active" if row.state == "issued" else row.state, "url": ""}
        if not row:
            row = TelegramActivation(reservation=current)
        row.actor = request.user
        row.state, row.proof_kind = "processing", kind
        row.evidence_ref = evidence
        row.phone_digest = salted_hmac("telegram-activation-phone.v1", phone).hexdigest()
        row.proof_id = uuid.uuid4()
        row.approve_authorized_at = row.issue_authorized_at = None
        row.expires_at = None
        row.save()
        AuditLog.objects.create(
            actor_user=request.user,
            action="telegram.activation_phone_proof",
            object_type="Reservation",
            object_reference=str(current.pk),
            summary="Booking-specific phone proof recorded; no message sent.",
            metadata={
                "proof_id": str(row.proof_id),
                "method": kind,
                "evidence_ref": evidence,
                "phone_digest": row.phone_digest,
            },
        )
    client = None
    state, url, expiry = "uncertain", "", None
    try:
        client = backend.Backend()
        client.authority_context = authority.booking_scope(
            request, current, admin=admin, row=row, phone=phone
        )
        if not admin and guest_proof(request, current) != proof:
            raise backend.NotReady
        client.prepare(key_for(current))
        approved = client.rpc(
            "tg20_s01_approve_phone",
            {
                "p_key": key_for(current),
                "p_phone": phone[1:],
                "p_evidence_ref": f"{kind}:{row.proof_id}",
            },
        )
        if approved["action"] == "approved":
            issued = client.rpc("tg20_s01_issue_invitation", {"p_key": key_for(current)})
            if issued["action"] == "issued":
                url, expiry = backend.checked_invitation(issued)
                state = "issued"
                if not admin and guest_proof(request, current) != proof:
                    state, url = "uncertain", ""
            else:
                state = "bound" if issued["action"] == "bound" else "hold"
        else:
            state = "hold"
    except (backend.NotReady, authority.Denied):
        state = "hold"
    except backend.Unavailable:
        pass  # No exception/provider body/contact/token is logged or returned.
    finally:
        if client:
            client.close()
    TelegramActivation.objects.filter(pk=row.pk).update(
        state=state, expires_at=expiry, updated_at=timezone.now()
    )
    # No raw capability persists in DB/cache/session/audit, including on replay.
    return {"state": state, "url": url, "expires_at": expiry}


def result_response(request, result):
    request._disable_google_integrations = True
    language = copy_for(request)
    result["copy"] = UI[language]
    result["message"] = STATES.get(language, LABELS).get(result["state"], LABELS["hold"])
    response = render(request, "reservations/telegram_activation_result.html", {"telegram": result})
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'none'; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    return response
