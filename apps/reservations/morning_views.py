"""Private operations screens for independent morning stays."""

import uuid
from datetime import datetime, time, timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.accounts.access import operations_access_required
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.notifications.services.audit import record_audit
from apps.payments.hyperpay.exceptions import HyperPayError
from apps.payments.morning_payments import public_url, verify_payment
from apps.properties.models import Property

from .models import MorningStay
from .morning_changes import cancel_stay, change_paid_stay
from .morning_stays import (
    ZONE,
    MorningStayError,
    active_stays,
    create_stay,
    property_zone,
    validate_window,
    verify_window,
)


class MorningAvailabilityForm(forms.Form):
    date = forms.DateField()
    start_time = forms.TimeField()
    end_time = forms.TimeField()
    guests = forms.IntegerField(min_value=1, max_value=20)

    def clean(self):
        data = super().clean()
        start, end = data.get("start_time"), data.get("end_time")
        if start is not None and end is not None and not time.min < start < end < time(12):
            raise forms.ValidationError("الدخول بعد منتصف الليل والخروج قبل ١٢ ظهرًا.")
        return data


class MorningAvailabilityReader:
    """Share date-window pages only within this read-only availability request."""

    def __init__(self, client):
        self.client = client
        self.pages = {}

    def get_listing_calendar(self, *args, **kwargs):
        return self.client.get_listing_calendar(*args, **kwargs)

    def guest_stay_page(self, departure_start, arrival_end, *, after_id=None):
        key = departure_start, arrival_end, after_id
        if key not in self.pages:
            self.pages[key] = self.client.guest_stay_page(
                departure_start, arrival_end, after_id=after_id
            )
        return self.pages[key]


@operations_access_required
def morning_available_properties(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    form = MorningAvailabilityForm(request.GET)
    if not form.is_valid():
        return JsonResponse(
            {"detail": "حدّد تاريخًا ووقت دخول وخروج صحيحين وعدد الضيوف."}, status=400
        )
    data = form.cleaned_data
    available, unverified = [], 0
    valid_windows = 0
    properties = Property.objects.filter(is_visible=True, hostaway_is_active=True).order_by(
        "sort_order", "id"
    )
    try:
        remote = HostawayClient(max_get_attempts=1, timeout=5)
    except HostawayError:
        return JsonResponse(
            {"detail": "تعذر الاتصال بمصدر التوفر؛ أعد المحاولة لاحقًا."}, status=503
        )
    with remote as client:
        reader = MorningAvailabilityReader(client)
        for property_obj in properties:
            try:
                zone = property_zone(property_obj)
                start = datetime.combine(data["date"], data["start_time"], zone)
                end = datetime.combine(data["date"], data["end_time"], zone)
                validate_window(start, end, zone=zone)
            except MorningStayError:
                continue
            valid_windows += 1
            try:
                if active_stays(property_obj, start, end + timedelta(hours=3)).exists():
                    continue
                evidence = verify_window(
                    property_obj, start, end, guests=data["guests"], client=reader
                )
            except MorningStayError:
                continue
            except HostawayError:
                unverified += 1
                continue
            available.append(
                {
                    "id": str(property_obj.pk),
                    "name": property_obj.name_ar or property_obj.name_en,
                    "time_zone": zone.key,
                    "starts_at": start.isoformat(),
                    "ends_at": end.isoformat(),
                    "checked_at": evidence["checked_at"],
                }
            )
    response = JsonResponse(
        {
            "properties": available,
            "unverified_count": unverified,
            "checked_at": timezone.now().isoformat(),
            "detail": (
                "وقت الدخول المحدد مضى أو خارج الأيام الأربعة عشر المقبلة؛ عدّل الوقت ثم تحقق."
                if properties and not valid_windows
                else ""
            ),
        }
    )
    response["Cache-Control"] = "no-store"
    return response


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
    guest_language = forms.ChoiceField(
        label="لغة رسائل الضيف",
        choices=[("ar", "العربية"), ("en", "English"), ("fr", "Français")],
        required=False,
        initial="ar",
    )
    guests = forms.IntegerField(label="عدد الضيوف", min_value=1, max_value=20, initial=1)
    amount_sar = forms.DecimalField(
        label="الإجمالي المعتمد بالريال السعودي — شامل الرسوم",
        min_value=1,
        max_value=9999999,
        max_digits=14,
        decimal_places=2,
    )

    def __init__(self, *args, **kwargs):
        self.stay = kwargs.pop("stay", None)
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

    def clean_guest_language(self):
        return self.cleaned_data.get("guest_language") or "ar"

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
                if not self.stay or (data["starts_at"], data["ends_at"]) != (
                    self.stay.starts_at,
                    self.stay.ends_at,
                ):
                    validate_window(
                        data["starts_at"],
                        data["ends_at"],
                        zone=zone,
                        allow_started=bool(self.stay and data["starts_at"] == self.stay.starts_at),
                    )
            except MorningStayError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return data


class MorningStayEditForm(MorningStayForm):
    expected_updated_at = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, stay, **kwargs):
        super().__init__(*args, stay=stay, **kwargs)
        del self.fields["request_id"]
        del self.fields["amount_sar"]
        local_start = stay.starts_at.astimezone(property_zone(stay.property))
        local_end = stay.ends_at.astimezone(property_zone(stay.property))
        self.initial.update(
            property=stay.property_id,
            date=local_start.date(),
            start_time=local_start.time().replace(tzinfo=None),
            end_time=local_end.time().replace(tzinfo=None),
            guest_name=stay.guest_name,
            guest_phone=stay.guest_phone,
            guest_email=stay.guest_email,
            guest_language=stay.guest_language,
            guests=stay.guests,
            expected_updated_at=stay.updated_at.isoformat(),
        )


@operations_access_required
def morning_edit(request, stay_id):
    stay = get_object_or_404(MorningStay.objects.select_related("property"), pk=stay_id)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form = MorningStayEditForm(request.POST or None, stay=stay)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            change_paid_stay(stay.pk, data, expected_updated_at=data["expected_updated_at"])
        except MorningStayError as exc:
            form.add_error(None, str(exc))
        except Exception:
            form.add_error(None, "تعذر التحقق من التعديل الآن؛ راجع التفاصيل قبل إعادة المحاولة.")
        else:
            record_audit(
                request=request,
                action="morning_stay.modified",
                object_type="MorningStay",
                object_reference=str(stay.pk),
                summary="Internal morning stay revised.",
            )
            messages.success(request, "حُفظ التعديل، وستُحدّث رسائل الحجز وتذكير الخروج.")
            return redirect("notifications:morning_detail", stay_id=stay.pk)
    return render(
        request,
        "admin/reservations/morning_create.html",
        context(request, title="تعديل الحجز الصباحي", form=form, editing=True),
    )


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
                guest_language=data.get("guest_language") or "ar",
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
            try:
                if stay.paid_at and request.POST.get("confirm_paid_cancellation") != "yes":
                    raise MorningStayError(
                        "أكد إلغاء الإقامة المدفوعة؛ الإلغاء لا ينفذ استردادًا ماليًا."
                    )
                cancel_stay(stay.pk)
            except MorningStayError as exc:
                messages.error(request, str(exc))
            else:
                record_audit(
                    request=request,
                    action="morning_stay.cancelled",
                    object_type="MorningStay",
                    object_reference=str(stay.pk),
                    summary="Morning stay cancelled; no automatic refund.",
                )
                messages.success(
                    request, "أُلغي الحجز وأوقفت رسائله المستقبلية؛ لم يُنفذ استرداد مالي."
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
