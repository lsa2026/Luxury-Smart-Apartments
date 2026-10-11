"""Owned checkout commands. No guest input can credit points or claim payment."""

import secrets
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.reservations.models import BookingIntent, Reservation
from apps.reservations.services.hostaway_booking import (
    HostawayBookingService,
    prepare_local_reservation,
)

from .services import available, hold, member_for, release
from .views import _actions_allowed, _throttle, _verified, interface_enabled

TEXT = {
    "ar": {
        "title": "استخدم نقاطك لهذه الإقامة",
        "available": "نقاطك المتاحة",
        "points": "النقاط المخصصة للحجز",
        "cash": "المبلغ المتبقي للدفع بالبطاقة بالريال",
        "apply": "استخدام النقاط",
        "remove": "إلغاء استخدام النقاط",
        "confirm": "تأكيد الحجز بالنقاط",
        "help": "كل نقطة تعادل ريالًا واحدًا. تبقى النقاط محجوزة حتى تأكيد الحجز.",
        "saved": "حُجزت النقاط لهذه الإقامة؛ راجع المبلغ المتبقي قبل الدفع.",
        "released": "أُعيدت النقاط المحجوزة إلى رصيدك المتاح.",
        "success": "تأكد حجزك بالنقاط دون خصم من البطاقة.",
        "review": "لم يتأكد الحجز بعد؛ لا تعِد المحاولة حتى حسم نتيجته.",
        "invalid": "تعذر تنفيذ الطلب. راجع النقاط وحالة الحجز أو تواصل معنا.",
    },
    "en": {
        "title": "Use your points for this stay",
        "available": "Available points",
        "points": "Points allocated to this booking",
        "cash": "Remaining card payment in SAR",
        "apply": "Apply points",
        "remove": "Remove points",
        "confirm": "Confirm with points",
        "help": (
            "One point equals one Saudi riyal. Points stay reserved until booking confirmation."
        ),
        "saved": "Points reserved. Review the remaining payment before continuing.",
        "released": "Reserved points are available again.",
        "success": "Your booking is confirmed with points. No card charge was made.",
        "review": "Confirmation needs review. Please do not retry until the outcome is resolved.",
        "invalid": "Unable to proceed. Check your points and booking status, or contact us.",
    },
    "fr": {
        "title": "Utilisez vos points pour ce séjour",
        "available": "Points disponibles",
        "points": "Points affectés à cette réservation",
        "cash": "Montant restant à régler par carte en riyals",
        "apply": "Utiliser mes points",
        "remove": "Retirer les points",
        "confirm": "Confirmer avec mes points",
        "help": (
            "Un point vaut un riyal saoudien. Les points restent réservés jusqu’à confirmation."
        ),
        "saved": "Points réservés. Vérifiez le montant restant avant de poursuivre.",
        "released": "Les points réservés sont de nouveau disponibles.",
        "success": "Votre séjour est confirmé avec vos points, sans débit de carte.",
        "review": "Confirmation à vérifier. Merci de ne pas renouveler la demande pour le moment.",
        "invalid": "Vérifiez vos points et votre réservation ou contactez-nous.",
    },
}


class PointsForm(forms.Form):
    points = forms.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.01"))
    request_reference = forms.CharField(max_length=80, min_length=16)


def checkout_context(request, intent):
    if not interface_enabled() or not _actions_allowed(request):
        return {}
    if not request.user.is_authenticated or intent.customer_id != request.user.pk:
        return {}
    if not _verified(request.user) or intent.status != BookingIntent.Status.AWAITING_PAYMENT:
        return {}
    if intent.currency != "SAR" or intent.payment_amount_sar != intent.total_price:
        return {}
    try:
        member = member_for(request.user)
        from .checkout import cash_due

        cash = cash_due(intent)
        state = (
            Reservation.objects.filter(booking_intent=intent)
            .values_list("loyalty_state", flat=True)
            .first()
            or {}
        )
        allocation = state.get("hold") or state.get("redemption") or {}
        return {
            "points_checkout": {
                "text": TEXT.get(intent.language, TEXT["ar"]),
                "available": available(member),
                "points": allocation.get("value", "0.00"),
                "cash": cash,
                "allocated": bool(allocation),
                "full_points": cash == 0,
                "frozen": bool(
                    state.get("checkout_frozen") or state.get("points_confirmation_started")
                ),
                "request_reference": secrets.token_urlsafe(24),
            }
        }
    except ValidationError:
        return {}


@never_cache
@login_required
@require_POST
def booking_points(request, public_reference):
    if not interface_enabled() or not _actions_allowed(request) or not _verified(request.user):
        raise PermissionDenied
    _throttle(request.user)
    intent = get_object_or_404(
        BookingIntent, public_reference=public_reference, customer=request.user
    )
    text = TEXT.get(intent.language, TEXT["ar"])
    try:
        if (
            intent.status != BookingIntent.Status.AWAITING_PAYMENT
            or intent.expires_at <= timezone.now()
        ):
            raise ValidationError("طلب الحجز غير صالح للدفع.")
        reservation = prepare_local_reservation(intent)
        action = request.POST.get("action")
        if action == "apply":
            form = PointsForm(request.POST)
            if not form.is_valid():
                raise ValidationError("قيمة غير صالحة.")
            hold(
                request.user,
                "checkout:" + form.cleaned_data["request_reference"],
                reservation.pk,
                form.cleaned_data["points"],
            )
            messages.success(request, text["saved"])
        elif action == "remove":
            reference = request.POST.get("request_reference", "")
            if not 16 <= len(reference) <= 80:
                raise ValidationError("مرجع الطلب مطلوب.")
            release(request.user, "checkout:" + reference, reservation.pk)
            messages.success(request, text["released"])
        elif action == "confirm":
            from .checkout import confirm_points_booking

            with HostawayBookingService() as service:
                result = confirm_points_booking(
                    request.user, reservation.pk, booking_service=service
                )
            if result["status"] in {"confirmed", "already_confirmed"}:
                messages.success(request, text["success"])
                return redirect(
                    "reservations:manage", public_reference=reservation.public_reference
                )
            messages.warning(request, text["review"])
        else:
            raise ValidationError("طلب غير معروف.")
    except ValidationError:
        messages.error(request, text["invalid"])
    return redirect("reservations:intent_detail", public_reference=intent.public_reference)
