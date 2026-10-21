"""Private operations screens for independent morning stays."""

import uuid
from datetime import datetime, time, timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.accounts.access import operations_access_required
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.notifications.services.audit import record_audit
from apps.payments.hyperpay.exceptions import HyperPayError
from apps.payments.morning_payments import public_url, verify_payment
from apps.properties.models import Property

from .models import MorningStay
from .morning_stays import (
    UNRESOLVED,
    ZONE,
    MorningStayError,
    create_stay,
    property_zone,
    validate_window,
)


class MorningStayForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    property = forms.ModelChoiceField(label="الشقة", queryset=Property.objects.none())
    date = forms.DateField(
        label="تاريخ الإقامة الصباحية", widget=forms.DateInput(attrs={"type": "date"})
    )
    start_time = forms.TimeField(
        label="ساعة الدخول — بتوقيت الشقة", widget=forms.TimeInput(attrs={"type": "time"})
    )
    end_time = forms.TimeField(
        label="ساعة الخروج — قبل ١٢ ظهرًا",
        initial=time(11),
        widget=forms.TimeInput(attrs={"type": "time"}),
    )
    guest_name = forms.CharField(label="اسم الضيف الكامل", max_length=200)
    guest_phone = forms.CharField(
        label="رقم الجوال الدولي",
        max_length=32,
        widget=forms.TextInput(attrs={"dir": "ltr", "placeholder": "+966…"}),
    )
    guest_email = forms.EmailField(label="البريد الإلكتروني")
    guests = forms.IntegerField(label="عدد الضيوف", min_value=1, max_value=20, initial=1)
    amount_sar = forms.DecimalField(
        label="الإجمالي المعتمد بالريال السعودي — شامل الرسوم",
        min_value=1,
        max_value=9999999,
        max_digits=14,
        decimal_places=2,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["property"].queryset = Property.objects.filter(
            is_visible=True,
            hostaway_is_active=True,
        ).order_by("sort_order", "id")
        if not self.is_bound:
            now = timezone.now().astimezone(ZONE) + timedelta(minutes=10)
            start = (
                now
                if now.hour < 11
                else datetime.combine(now.date() + timedelta(days=1), time(1), ZONE)
            )
            self.initial.update(
                request_id=uuid.uuid4(),
                date=start.date(),
                start_time=start.time().replace(second=0, microsecond=0),
            )

    def clean_guest_phone(self):
        try:
            return normalize_phone_number(self.cleaned_data["guest_phone"])
        except InvalidPhoneNumber as exc:
            raise forms.ValidationError("أدخل رقمًا دوليًا صحيحًا.") from exc

    def clean_guest_name(self):
        name = " ".join(self.cleaned_data["guest_name"].split())
        if len(name.split()) < 2:
            raise forms.ValidationError("أدخل الاسم الأول واسم العائلة كما يلزمان للدفع.")
        return name

    def clean(self):
        data = super().clean()
        if all(data.get(k) is not None for k in ("property", "date", "start_time", "end_time")):
            try:
                zone = property_zone(data["property"])
                data["starts_at"] = datetime.combine(data["date"], data["start_time"], zone)
                data["ends_at"] = datetime.combine(data["date"], data["end_time"], zone)
                validate_window(data["starts_at"], data["ends_at"], zone=zone)
            except MorningStayError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return data


def context(request, **fields):
    from django.contrib import admin

    return {**admin.site.each_context(request), **fields}


@operations_access_required
def morning_list(request):
    return render(
        request,
        "admin/reservations/morning_list.html",
        context(
            request,
            title="الحجوزات الصباحية",
            stays=MorningStay.objects.select_related("property").order_by("-created_at")[:100],
        ),
    )


@operations_access_required
def morning_create(request):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form = MorningStayForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            stay = create_stay(
                actor=request.user,
                property_obj=data["property"],
                **{
                    k: data[k]
                    for k in (
                        "request_id",
                        "starts_at",
                        "ends_at",
                        "guest_name",
                        "guest_phone",
                        "guest_email",
                        "guests",
                        "amount_sar",
                    )
                },
            )
        except MorningStayError as exc:
            form.add_error(None, str(exc))
        except Exception:
            form.add_error(
                None, "تعذر التحقق من التوفر الآن. لم يُنشأ حجز أو رابط دفع؛ أعد المحاولة لاحقًا."
            )
        else:
            record_audit(
                request=request,
                action="morning_stay.created",
                object_type="MorningStay",
                object_reference=str(stay.pk),
                summary="Internal morning stay created; no Hostaway write or guest delivery.",
            )
            return redirect("notifications:morning_detail", stay_id=stay.pk)
    return render(
        request,
        "admin/reservations/morning_create.html",
        context(request, title="إنشاء حجز صباحي", form=form),
    )


@operations_access_required
def morning_detail(request, stay_id):
    stay = get_object_or_404(MorningStay.objects.select_related("property"), pk=stay_id)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "cancel":
            with transaction.atomic():
                Property.objects.select_for_update().get(pk=stay.property_id)
                stay = MorningStay.objects.select_for_update().get(pk=stay.pk)
                if (
                    stay.paid_at
                    or stay.status in {"paid", "processing", "review"}
                    or stay.attempts.filter(status__in=UNRESOLVED).exists()
                ):
                    messages.error(
                        request,
                        "توجد دفعة أو محاولة غير محسومة؛ لا يمكن تحرير الإشغال قبل مراجعتها.",
                    )
                else:
                    stay.status = "cancelled"
                    stay.save(update_fields=["status", "updated_at"])
                    record_audit(
                        request=request,
                        action="morning_stay.cancelled",
                        object_type="MorningStay",
                        object_reference=str(stay.pk),
                        summary="Unpaid morning request cancelled.",
                    )
        elif action == "verify":
            attempt = stay.attempts.first()
            if attempt and attempt.provider_checkout_id:
                try:
                    verify_payment(attempt.pk)
                    messages.success(request, "تمت قراءة حالة الدفع من مزود الدفع.")
                except HyperPayError:
                    messages.error(request, "تعذر حسم الدفع الآن؛ لا تطلب دفعًا آخر قبل التحقق.")
        return redirect("notifications:morning_detail", stay_id=stay.pk)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return render(
        request,
        "admin/reservations/morning_detail.html",
        context(
            request,
            title="تفاصيل الحجز الصباحي",
            stay=stay,
            payment_url=public_url(stay)
            if settings.HYPERPAY_ENABLED and stay.status in {"open", "processing"}
            else "",
            attempts=stay.attempts.all(),
            expired=stay.status == "open" and stay.expires_at <= timezone.now(),
        ),
    )
