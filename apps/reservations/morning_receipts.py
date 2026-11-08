"""Audited MorningStay collection only; never a provider charge or guest send."""

from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.access import require_operations_access
from apps.notifications.services.audit import record_audit
from apps.properties.models import Property

from .models import MorningOfflineReceipt, MorningStay


class MorningReceiptForm(forms.Form):
    method = forms.ChoiceField(
        label="طريقة السداد", choices=MorningOfflineReceipt._meta.get_field("method").choices
    )
    amount = forms.DecimalField(
        label="المبلغ المستلم بالريال السعودي",
        min_value=Decimal("0.01"),
        max_digits=14,
        decimal_places=2,
    )
    received_at = forms.DateTimeField(
        label="وقت الاستلام — بتوقيت الرياض",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    reference = forms.CharField(
        label="مرجع الحوالة أو سند القبض (اختياري)", max_length=100, required=False
    )
    note = forms.CharField(
        label="ملاحظة (مثل اسم البنك) — اختيارية", max_length=500, required=False
    )
    confirmed = forms.BooleanField(
        label="أؤكد استلام كامل المبلغ فعليًا والتحقق من الحوالة أو النقد"
    )


def record_receipt(
    *, request, stay_id, amount, method, received_at, reference="", note="", confirmed=False
):
    require_operations_access(request)
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("مبلغ الاستلام غير صحيح.") from exc
    if (
        not value.is_finite()
        or value <= 0
        or value.as_tuple().exponent < -2
        or method not in {"bank_transfer", "cash"}
        or confirmed is not True
        or received_at is None
        or timezone.is_naive(received_at)
        or received_at > timezone.now()
        or len(reference) > 100
        or len(note) > 500
    ):
        raise ValidationError("أكد الاستلام الفعلي وأدخل مبلغًا ووقتًا وطريقة سداد صحيحة.")
    with transaction.atomic():
        property_id = MorningStay.objects.values_list("property_id", flat=True).get(pk=stay_id)
        Property.objects.select_for_update().get(pk=property_id)
        stay = MorningStay.objects.select_for_update().get(pk=stay_id)
        fields = dict(
            amount=value,
            method=method,
            received_at=received_at,
            reference=reference.strip(),
            note=note.strip(),
        )
        existing = MorningOfflineReceipt.objects.filter(stay=stay).first()
        if existing:
            if all(getattr(existing, k) == v for k, v in fields.items()):
                return existing
            raise ValidationError("يوجد سند سداد لهذا الحجز؛ راجع السجل دون تسجيل سند آخر.")
        if stay.paid_at or stay.status == "cancelled" or value != stay.amount_sar:
            raise ValidationError("يلزم حجز غير ملغى وغير مسدد، ومبلغ مطابق للإجمالي المعتمد.")
        if stay.attempts.filter(
            status__in=["created", "pending", "unknown", "review", "succeeded"]
        ).exists():
            raise ValidationError(
                "توجد محاولة دفع ناجحة أو غير محسومة. حدّث حالتها وتحقق منها قبل توثيق سداد آخر."
            )
        receipt = MorningOfflineReceipt.objects.create(
            stay=stay, recorded_by=request.user, **fields
        )
        # Explicit financial record only: do not invoke save's communication hooks.
        MorningStay.objects.filter(pk=stay.pk).update(
            status="paid", paid_at=received_at, updated_at=timezone.now()
        )
        from .morning_loyalty import queue_paid_invitation

        stay.refresh_from_db()
        queue_paid_invitation(stay)
        record_audit(
            request=request,
            action="morning_stay.offline_received",
            object_type="MorningOfflineReceipt",
            object_reference=str(receipt.pk),
            summary="توثيق سداد صباحي مستلم فعليًا بحوالة بنكية أو نقدًا؛ دون تحصيل جديد.",
            metadata={"source": method, "status": "paid"},
        )
        return receipt
