"""Existing single-owner gate; factual attestations, not a discretionary award UI."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.integrations.hostaway.exceptions import HostawayError
from apps.reservations.models import Reservation

from .launch import booking_allowed
from .localization import arabic_admin
from .settlements import record_contract_nights, record_manual_receipt
from .stay import record_departure
from .views import _actions_allowed, _owner, preview_mode


@arabic_admin
@never_cache
@staff_member_required
@require_POST
def settlement_command(request):
    _owner(request)
    if not _actions_allowed(request):
        raise PermissionDenied
    booking = get_object_or_404(
        Reservation,
        public_reference=request.POST.get("booking", ""),
        source_type=Reservation.SourceType.DIRECT_WEBSITE,
    )
    if preview_mode() and not booking.is_test:
        raise PermissionDenied
    if not booking_allowed(booking):
        raise PermissionDenied
    try:
        reference = request.POST.get("reference", "").strip()[:255]
        action = request.POST.get("action")
        if action == "recover":
            # A local UI preview must never open a live provider connection.
            if preview_mode():
                raise ValidationError("حسم الحجز الخارجي غير متاح في المعاينة المحلية.")
            from apps.integrations.hostaway.client import HostawayClient

            from .recovery import recover_points_booking

            with HostawayClient() as client:
                recover_points_booking(
                    request.user,
                    booking.pk,
                    external_id=int(request.POST.get("external_id", "")),
                    reference=reference,
                    guest_identity_confirmed=request.POST.get("identity_confirmed") == "on",
                    client=client,
                )
            messages.success(request, "تأكد الحجز الموجود وصُرفت نقاطه دون تحصيل جديد.")
        elif action == "nights":
            detail = request.POST.get("nights", "")
            if len(detail) > 50000:
                raise ValidationError("تفصيل طويل جدًا.")
            nights = []
            for line in detail.splitlines():
                if not line.strip():
                    continue
                day, value = line.split("|")
                nights.append({"date": day.strip(), "value": value.strip()})
            record_contract_nights(request.user, booking.pk, nights=nights, reference=reference)
            messages.success(request, "حُفظت أسعار ليالي العقد ومرجعها.")
        elif action in {"receipt", "departure"}:
            moment = datetime.fromisoformat(request.POST.get("at", ""))
            if timezone.is_naive(moment):
                moment = moment.replace(tzinfo=ZoneInfo(booking.property.time_zone_name))
            if action == "receipt":
                record_manual_receipt(
                    request.user,
                    booking.pk,
                    value=request.POST.get("value", ""),
                    received_at=moment,
                    reference=reference,
                    lodging_only=request.POST.get("lodging_only") == "on",
                )
                messages.success(request, "وُثق القبض الفعلي؛ لم ينفذ أي تحصيل جديد.")
            else:
                plan = record_departure(
                    request.user, booking.pk, vacated_at=moment, reference=reference
                )
                messages.success(
                    request,
                    f"وُثقت المغادرة: {plan['cash_refund']} ريال و{plan['points_refund']} نقطة. "
                    "تنفيذ الاسترداد بعد تأكيد الإلغاء.",
                )
        else:
            raise ValidationError("طلب غير معروف.")
    except (ValueError, KeyError, TypeError, ValidationError, HostawayError):
        messages.error(
            request, "لم يُحفظ الطلب؛ تحقق من الحجز والوقت والمبلغ والمرجع وتفصيل الليالي."
        )
    return redirect("loyalty:admin")
