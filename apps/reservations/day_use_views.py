import uuid
from datetime import datetime, time, timedelta
from functools import wraps

import phonenumbers
from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core import signing
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone, translation

from apps.accounts.access import operations_access_required
from apps.core.phone_numbers import InvalidPhoneNumber
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.notifications.services.audit import record_audit
from apps.payments.morning_validation import MorningBillingFields as DayUseBillingFields
from apps.payments.morning_validation import validate_value
from apps.properties.models import Property

from .booking_forms import GuestDetailsForm
from .day_use_billing import DayUseBillingForm
from .day_use_hostaway import booking_state, record_in_hostaway
from .day_use_stays import (
    ZONE,
    DayUseStayError,
    active_stays,
    create_stay,
    property_zone,
    validate_window,
    verify_window,
)
from .models import DayUseStay
from .morning_phone import normalize_morning_phone as normalize_day_use_phone


def day_use_arabic_ui(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        request.LANGUAGE_CODE = "ar"
        with translation.override("ar"):
            return view(request, *args, **kwargs)

    return wrapped


class DayUseAvailabilityForm(forms.Form):
    date = forms.DateField()
    start_time = forms.TimeField()
    end_time = forms.TimeField()
    guests = forms.IntegerField(min_value=1, max_value=20)

    def clean(self):
        data = super().clean()
        start, end = data.get("start_time"), data.get("end_time")
        if start is not None and end is not None and not start < end:
            raise forms.ValidationError("الخروج بعد الدخول في اليوم نفسه.")
        return data


class DayUseAvailabilityReader:
    """Share date-window pages only within this read-only availability request."""

    def __init__(self, client):
        self.client = client
        self.pages = {}

    def __getattr__(self, name):
        return getattr(self.client, name)

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
def day_use_available_properties(request):
    if not settings.DAY_USE08_ENABLED:
        return JsonResponse({"detail": "المسار النهاري لم يُفعّل بعد."}, status=503)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    form = DayUseAvailabilityForm(request.GET)
    if not form.is_valid():
        return JsonResponse(
            {"detail": "حدّد تاريخًا ووقت دخول وخروج صحيحين وعدد الضيوف."}, status=400
        )
    data = form.cleaned_data
    available, excluded, unverified = [], [], 0
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
        reader = DayUseAvailabilityReader(client)
        for property_obj in properties:
            try:
                zone = property_zone(property_obj)
                start = datetime.combine(data["date"], data["start_time"], zone)
                end = datetime.combine(data["date"], data["end_time"], zone)
                validate_window(start, end, zone=zone, allow_started=False)
            except DayUseStayError:
                continue
            valid_windows += 1
            try:
                if active_stays(property_obj, start, end + timedelta(hours=3)).exists():
                    excluded.append(
                        {"name": str(property_obj), "reason": "يوجد طلب نهاري آخر متداخل للشقة."}
                    )
                    continue
                evidence = verify_window(
                    property_obj,
                    start,
                    end,
                    guests=data["guests"],
                    client=reader,
                    allow_started=False,
                )
            except DayUseStayError as exc:
                excluded.append({"name": str(property_obj), "reason": str(exc)})
                continue
            except HostawayError:
                excluded.append(
                    {
                        "name": str(property_obj),
                        "reason": "تعذر التحقق من مصدر الحجوزات؛ لم يُثبت التوفر.",
                    }
                )
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
                    "amount_sar": evidence["pricing"]["amount_sar"],
                    "quote_token": quote_token(
                        property_obj, start, end, data["guests"], evidence["pricing"]
                    ),
                }
            )
    response = JsonResponse(
        {
            "properties": available,
            "excluded_properties": excluded,
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


class DayUseStayForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    property = forms.ModelChoiceField(label="الشقة", queryset=Property.objects.none())
    date = forms.DateField(
        label="تاريخ الإقامة النهارية", widget=forms.DateInput(attrs={"type": "date"})
    )
    start_time = forms.TimeField(
        label="ساعة الدخول — بتوقيت الشقة", widget=forms.TimeInput(attrs={"type": "time"})
    )
    end_time = forms.TimeField(
        label="ساعة الخروج",
        initial=time(20),
        widget=forms.TimeInput(attrs={"type": "time"}),
    )
    guest_name = forms.CharField(label="اسم الضيف الكامل", max_length=200)
    guest_phone = forms.CharField(
        label="رقم الجوال الدولي",
        max_length=32,
        widget=forms.TextInput(attrs={"dir": "ltr", "placeholder": "+966…"}),
    )
    guest_phone_country = forms.ChoiceField(required=False)
    guest_email = forms.EmailField(label="البريد الإلكتروني")
    guest_language = forms.ChoiceField(
        label="لغة رسائل الضيف",
        choices=[("ar", "العربية"), ("en", "English"), ("fr", "Français")],
        required=False,
        initial="ar",
    )
    guests = forms.IntegerField(label="عدد الضيوف", min_value=1, max_value=20, initial=1)
    quote_token = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        self.require_address = kwargs.pop("require_address", False)
        self.stay = kwargs.pop("stay", None)
        super().__init__(*args, **kwargs)
        with translation.override("ar"):
            shared_phone = GuestDetailsForm()
            self.fields["guest_email"] = DayUseBillingFields(customer=True).fields["guest_email"]
            self.fields["guest_phone"] = shared_phone.fields["guest_phone"]
            self.fields["guest_phone_country"] = shared_phone.fields["guest_phone_country"]
        self.fields["guest_phone_country"].label = "دولة رقم الجوال"
        self.fields["guest_phone"].label = "رقم الجوال"
        self.fields["guest_name"].help_text = (
            "أدخل الاسم الأول واسم العائلة الحقيقيين. "
            "يمكن للضيف مراجعة بيانات الدافع وتصحيحها قبل الدفع."
        )
        if not self.stay:
            self.initial.setdefault("billing_country", "SA")
            with translation.override("ar"):
                billing_fields = DayUseBillingForm(default_country_code="SA").fields
            for name, field in billing_fields.items():
                field.required = self.require_address
                self.fields[name] = field
            if self.require_address:
                self.fields["confirm_hostaway"] = forms.BooleanField(
                    label=(
                        "أؤكد بيانات الضيف والفترة والمبلغ، وأوافق على إنشاء الحجز في هوستاوي الآن."
                    )
                )
        self.fields["property"].queryset = Property.objects.filter(
            is_visible=True,
            hostaway_is_active=True,
        ).order_by("sort_order", "id")
        if not self.is_bound:
            self.initial.setdefault("guest_phone_country", "SA")
            if self.stay and self.stay.guest_phone:
                try:
                    parsed = phonenumbers.parse(self.stay.guest_phone, None)
                    region = phonenumbers.region_code_for_number(parsed)
                    if region in phonenumbers.SUPPORTED_REGIONS:
                        self.initial["guest_phone_country"] = region
                except phonenumbers.NumberParseException:
                    pass
            day = timezone.now().astimezone(ZONE).date() + timedelta(days=1)
            self.initial.update(request_id=uuid.uuid4(), date=day, start_time=time(9))

    def clean_guest_phone(self):
        try:
            country = self.data.get("guest_phone_country") or "SA"
            return normalize_day_use_phone(self.cleaned_data["guest_phone"], country)
        except InvalidPhoneNumber as exc:
            raise forms.ValidationError("تحقق من رقم الجوال والدولة المختارة.") from exc

    def clean_guest_language(self):
        return self.cleaned_data.get("guest_language") or "ar"

    def clean_guest_name(self):
        name = " ".join(self.cleaned_data["guest_name"].split())
        if len(name.split()) < 2:
            raise forms.ValidationError("أدخل الاسم الأول واسم العائلة كما يلزمان للدفع.")
        return name

    def clean_guest_email(self):
        return validate_value("guest_email", self.cleaned_data["guest_email"])

    def clean(self):
        data = super().clean()
        if not self.stay and (
            self.require_address
            or any(
                data.get(k)
                for k in self.fields
                if k.startswith("billing_") and k != "billing_country"
            )
        ):
            billing = DayUseBillingForm(data)
            if billing.is_valid():
                data["billing_address"] = dict(billing.cleaned_data)
            else:
                for name, errors in billing.errors.items():
                    self.add_error(name, errors)
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
                        allow_started=False,
                    )
            except DayUseStayError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return data


QUOTE_SALT = "day-use08.admin-quote.v1"


def quote_token(property_obj, start, end, guests, pricing):
    return signing.dumps(
        {
            "property": property_obj.pk,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "guests": guests,
            "pricing": pricing,
        },
        salt=QUOTE_SALT,
    )


def reviewed_quote(data):
    try:
        value = signing.loads(data["quote_token"], salt=QUOTE_SALT, max_age=300)
        if any(
            (
                value["property"] != data["property"].pk,
                value["start"] != data["starts_at"].isoformat(),
                value["end"] != data["ends_at"].isoformat(),
                value["guests"] != data["guests"],
            )
        ):
            raise ValueError
        return value["pricing"]
    except (signing.BadSignature, ValueError, KeyError, TypeError) as exc:
        raise DayUseStayError("أعد فحص التوفر واختيار الشقة والسعر قبل التأكيد.") from exc


def context(request, **values):
    return {**admin.site.each_context(request), "current_language_code": "ar", **values}


@operations_access_required
@day_use_arabic_ui
def day_use_list(request):
    return render(
        request,
        "admin/reservations/day_use_list.html",
        context(
            request,
            title="الحجوزات النهارية",
            stays=DayUseStay.objects.select_related("property")[:100],
        ),
    )


@operations_access_required
@day_use_arabic_ui
def day_use_create(request):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    if not settings.DAY_USE08_ENABLED:
        return render(
            request,
            "admin/reservations/day_use_create.html",
            context(
                request,
                title="حجز نهاري جديد",
                enabled=False,
                form=DayUseStayForm(require_address=True),
            ),
        )
    form = DayUseStayForm(request.POST or None, require_address=True)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            pricing = reviewed_quote(data)
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
                        "guest_language",
                        "guests",
                        "billing_address",
                    )
                },
                expected_pricing=pricing,
            )
        except DayUseStayError as exc:
            form.add_error(None, str(exc))
        except Exception:
            form.add_error(None, "تعذر التحقق من التوفر والسعر؛ أعد الفحص قبل المحاولة.")
        else:
            try:
                record_in_hostaway(request=request, stay_id=stay.pk)
            except Exception:
                messages.error(request, "لم يتأكد إنشاء الحجز. راجع حالة الربط؛ لا تكرر الإنشاء.")
            else:
                messages.success(
                    request, "تأكد الحجز في هوستاوي وحُفظ حدث المسار الثامن. الدفع مستقل."
                )
            record_audit(
                request=request,
                action="day_use_stay.created",
                object_type="DayUseStay",
                object_reference=str(stay.pk),
                summary="Day-use manager confirmation processed.",
            )
            return redirect("notifications:day_use_detail", stay_id=stay.pk)
    return render(
        request,
        "admin/reservations/day_use_create.html",
        context(request, title="حجز نهاري جديد", form=form, enabled=True),
    )


@operations_access_required
@day_use_arabic_ui
def day_use_detail(request, stay_id):
    stay = get_object_or_404(DayUseStay.objects.select_related("property"), pk=stay_id)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    if request.method == "POST":
        if request.POST.get("action") == "correct_phone":
            try:
                from .day_use_phone import correct_phone

                phone = normalize_day_use_phone(
                    request.POST.get("phone", ""), request.POST.get("country") or "SA"
                )
                correct_phone(
                    stay.pk, phone, expected_updated_at=request.POST.get("expected_updated_at", "")
                )
            except (DayUseStayError, InvalidPhoneNumber) as exc:
                messages.error(request, str(exc))
            else:
                record_audit(
                    request=request,
                    action="day_use_stay.phone_corrected",
                    object_type="DayUseStay",
                    object_reference=str(stay.pk),
                    summary="Day-use phone corrected with provider readback.",
                )
                messages.success(request, "حُفظ التصحيح وتحديث المسار الثامن.")
        return redirect("notifications:day_use_detail", stay_id=stay.pk)
    return render(
        request,
        "admin/reservations/day_use_detail.html",
        context(request, title="تفاصيل الحجز النهاري", stay=stay, operation=booking_state(stay)),
    )
