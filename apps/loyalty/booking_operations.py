"""Booking-bound stay controls; previews never alter money, points or inventory."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts.access import is_operations_owner
from apps.notifications.models import AuditLog
from apps.reservations.models import BookingModificationRequest, Reservation

from .accrual import policy_enabled
from .calculation import amount, cents
from .launch import booking_allowed
from .localization import arabic_admin
from .services import confirm_arrival, digest, posting_transaction, reconcile
from .site_adapter import stay_times
from .stay import departure_plan, record_departure
from .views import _actions_allowed, _owner, preview_mode

SALT = "loyalty-booking-end-v1"


def stay_controls(user, booking):
    if not (
        is_operations_owner(user)
        and settings.LOYALTY_ENABLED
        and settings.LOYALTY_UI_ENABLED
        and booking.booking_intent_id
        and booking.source_type == Reservation.SourceType.DIRECT_WEBSITE
        and booking_allowed(booking)
    ):
        return None
    try:
        start, end = stay_times(booking)
    except (ValidationError, ValueError, KeyError):
        return None
    active = booking.normalized_status in Reservation.ACTIVE_STATUSES
    arrival = booking.loyalty_state.get("arrival")
    departure = booking.loyalty_state.get("departure_settlement")
    pending = (
        booking.modification_requests.filter(
            request_type=BookingModificationRequest.RequestType.CANCEL_RESERVATION
        )
        .order_by("-requested_at")
        .first()
        if departure and not departure.get("normal")
        else None
    )
    return {
        "arrived": bool(arrival),
        "departure": departure,
        "pending": pending,
        "can_arrive": active
        and not arrival
        and (policy_enabled() or start <= timezone.now() < end),
        "scheduled_accrual": bool(booking.loyalty_state.get("scheduled_accrual")),
        "can_end": active and bool(arrival) and not departure,
        "not_started": timezone.now() < start,
    }


def _snapshot(booking):
    return digest(
        {
            "booking": str(booking.pk),
            "status": booking.normalized_status,
            "start": booking.check_in,
            "end": booking.check_out,
            "total": booking.total_price,
            "updated": booking.updated_at,
            "state": booking.loyalty_state,
            "payments": list(
                booking.booking_intent.payment_attempts.order_by("pk").values(
                    "pk", "amount", "status", "verified_at"
                )
            ),
            "refunds": list(
                booking.refund_obligations.order_by("pk").values("pk", "amount", "status")
            ),
        }
    )


def _moment(request, booking):
    value = request.POST.get("vacated_at", "").strip()
    moment = parse_datetime(value) if value else timezone.now()
    if moment is None:
        raise ValidationError("تحقق من وقت المغادرة الفعلي.")
    if timezone.is_naive(moment):
        moment = moment.replace(tzinfo=ZoneInfo(booking.property.time_zone_name))
    return moment


@arabic_admin
@never_cache
@staff_member_required
@require_POST
def booking_stay(request, reservation_id):
    _owner(request)
    if not _actions_allowed(request):
        raise PermissionDenied
    booking = get_object_or_404(
        Reservation.objects.select_related(
            "property", "booking_intent__customer", "booking_intent__quote"
        ),
        pk=reservation_id,
        source_type=Reservation.SourceType.DIRECT_WEBSITE,
    )
    if not booking_allowed(booking) or (preview_mode() and not booking.is_test):
        raise PermissionDenied
    destination = "notifications:booking_detail"
    try:
        action = request.POST.get("action")
        if action == "arrival":
            if booking.loyalty_state.get("arrival"):
                messages.info(request, "الوصول موثق مسبقًا؛ لم تُضف مكافأة مكررة.")
            else:
                result = confirm_arrival(
                    request.user,
                    booking.pk,
                    arrived_at=timezone.now(),
                    reference=f"booking-arrival:{booking.pk}",
                )
                messages.success(
                    request, "تم تأكيد وصول الضيف. احتساب النقاط تلقائي بعد اكتمال الشروط."
                )
                if result.get("status") not in {"settled", "unchanged"}:
                    messages.info(
                        request,
                        "إذا لم تظهر النقاط، تحقق من الانضمام والسداد الإلكتروني الكامل "
                        "وأهلية الحجز.",
                    )
        elif action == "preview":
            if booking.loyalty_state.get("departure_settlement"):
                raise ValidationError("المغادرة موثقة بالفعل؛ راجع حالتها داخل الحجز.")
            if booking.normalized_status not in Reservation.ACTIVE_STATUSES:
                raise ValidationError("الحجز ليس في حالة نشطة.")
            moment = _moment(request, booking)
            plan = departure_plan(booking, vacated_at=moment)
            token = signing.dumps(
                {
                    "booking": str(booking.pk),
                    "user": request.user.pk,
                    "at": moment.isoformat(),
                    "snapshot": _snapshot(booking),
                },
                salt=SALT,
            )
            return render(
                request,
                "loyalty/booking_end.html",
                {
                    "title": "مراجعة إنهاء الإقامة",
                    "reservation": booking,
                    "plan": plan,
                    "moment": timezone.localtime(moment, ZoneInfo(booking.property.time_zone_name)),
                    "reward_after": amount(cents(plan["consumed_cash_lodging"]) * 3 // 100),
                    "consumed": booking.total_price - Decimal(plan["refundable_total"]),
                    "confirmation": token,
                },
            )
        elif action == "end":
            payload = signing.loads(request.POST.get("confirmation", ""), salt=SALT, max_age=600)
            if (payload.get("booking"), payload.get("user")) != (str(booking.pk), request.user.pk):
                raise PermissionDenied
            if request.POST.get("confirm_vacated") != "on":
                raise ValidationError("أكد أن الضيف أخلى الشقة فعليًا قبل متابعة الإنهاء.")
            with posting_transaction():
                booking = (
                    Reservation.objects.select_for_update(of=("self",))
                    .select_related("property", "booking_intent__customer", "booking_intent__quote")
                    .get(pk=booking.pk)
                )
                if booking.loyalty_state.get("departure_settlement"):
                    messages.info(request, "المغادرة موثقة مسبقًا؛ لم نكرر التسوية.")
                    return redirect(destination, reservation_id=booking.pk)
                if payload["snapshot"] != _snapshot(booking):
                    raise ValidationError(
                        "تغيرت بيانات الحجز أو السداد؛ أعد مراجعة الحساب قبل التأكيد."
                    )
                plan = record_departure(
                    request.user,
                    booking.pk,
                    vacated_at=datetime.fromisoformat(payload["at"]),
                    reference=f"booking-end:{booking.pk}",
                )
                if not plan["normal"]:
                    from apps.reservations.services.modifications import ModificationService

                    outcome = ModificationService.create_cancellation_request(
                        booking,
                        session_hash=f"owner:{request.user.pk}",
                        reason="إنهاء إقامة فعلية مع احتساب الليالي غير المستخدمة",
                        owner_override=True,
                    )
                    if outcome.request is None:
                        raise ValidationError(
                            "تعذر تجهيز مسار الإلغاء. لم يُنفذ إنهاء أو استرداد؛ راجع إعدادات الحجز."
                        )
                AuditLog.objects.create(
                    actor_user=request.user,
                    action="loyalty.booking_departure",
                    object_type="Reservation",
                    object_reference=str(booking.pk),
                    summary="تأكيد مغادرة فعلية من داخل الحجز بعد مراجعة الحساب",
                    metadata={"normal": plan["normal"], "at": payload["at"]},
                )
            if plan["normal"]:
                reconcile(booking.pk)
                messages.success(
                    request,
                    "تم إنهاء الإقامة وتحديث احتساب المكافأة. لا توجد ليالٍ متبقية لاستردادها.",
                )
            else:
                messages.success(
                    request,
                    "حُفظت المغادرة وخطة المبلغ المتبقي. أكد الإلغاء والاسترداد في الخطوة التالية؛ "
                    "تُصحح النقاط بعد تأكيد الإلغاء.",
                )
                return redirect("notifications:cancellation_detail", request_id=outcome.request.pk)
        else:
            raise ValidationError("اختر الإجراء من داخل الحجز.")
    except signing.BadSignature:
        messages.error(request, "انتهت صلاحية المراجعة؛ افتح الحجز وأعد حساب إنهاء الإقامة.")
    except (ValidationError, ValueError, KeyError) as exc:
        text = (
            "؛ ".join(exc.messages)
            if isinstance(exc, ValidationError)
            else "تحقق من بيانات الإقامة وأعد المحاولة."
        )
        messages.error(request, text)
    return redirect(destination, reservation_id=booking.pk)
