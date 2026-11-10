"""Shared billing fields for an independently identified daytime stay."""

from .morning_billing import MorningBillingForm as DayUseBillingForm
from .morning_billing import saved_billing as saved_billing

__all__ = ["DayUseBillingForm", "saved_billing", "correct_billing"]


def correct_billing(stay_id, data, *, expected_updated_at):
    """Correct only local billing, serialized with checkout; no provider IO or replay."""
    from django.conf import settings
    from django.db import transaction
    from django.utils import timezone

    from .day_use_hostaway import _locked, booking_state
    from .day_use_stays import UNRESOLVED, DayUseStayError

    form = DayUseBillingForm(data)
    if not form.is_valid():
        raise DayUseStayError("تحقق من شروط حقول عنوان الفوترة قبل الحفظ.")
    with transaction.atomic():
        stay = _locked(stay_id)
        operation = booking_state(stay)
        if (
            not settings.DAY_USE08_ENABLED
            or stay.updated_at.isoformat() != expected_updated_at
            or stay.status not in {"open", "expired"}
            or stay.paid_at
            or stay.ends_at <= timezone.now()
            or operation.get("state") not in {None, "confirmed"}
            or stay.attempts.filter(status__in=(*UNRESOLVED, "succeeded")).exists()
            or stay.payment_events.exists()
        ):
            raise DayUseStayError(
                "تغيّرت حالة الحجز أو توجد عملية دفع تحتاج مراجعة؛ لم يُحفظ تغيير العنوان."
            )
        stay.billing_address = dict(form.cleaned_data)
        stay.save(update_fields=["billing_address", "updated_at"])
        return stay
