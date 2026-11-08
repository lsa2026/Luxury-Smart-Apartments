"""Private operations screens for independent morning stays."""

import uuid
from datetime import datetime, time, timedelta
from functools import wraps

import phonenumbers
from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone, translation

from apps.accounts.access import operations_access_required
from apps.core.phone_numbers import InvalidPhoneNumber
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.notifications.services.audit import record_audit
from apps.payments.hyperpay.exceptions import HyperPayError
from apps.payments.morning_payments import public_url, verify_payment
from apps.payments.morning_validation import MorningBillingFields, validate_value
from apps.properties.models import Property

from . import morning_payment_messages as payment_messages
from .booking_forms import GuestDetailsForm
from .models import MorningStay
from .morning07_changes import cancel_stay, change_paid_stay
from .morning_billing import MorningBillingForm, address_context, address_view
from .morning_hostaway import booking_state, record_in_hostaway
from .morning_phone import normalize_morning_phone
from .morning_receipts import MorningReceiptForm, record_receipt
from .morning_stays import (
    ZONE,
    MorningStayError,
    active_stays,
    create_stay,
    property_zone,
    validate_window,
    verify_window,
)


def morning_arabic_ui(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        request.LANGUAGE_CODE = "ar"
        with translation.override("ar"):
            return view(request, *args, **kwargs)

    return wrapped


class MorningAvailabilityForm(forms.Form):
    date = forms.DateField()
    start_time = forms.TimeField()
    end_time = forms.TimeField()
    guests = forms.IntegerField(min_value=1, max_value=20)

    def clean(self):
        data = super().clean()
        start, end = data.get("start_time"), data.get("end_time")
        if start is not None and end is not None and not time.min < start < end <= time(12):
            raise forms.ValidationError("الدخول بعد منتصف الليل والخروج حتى ١٢ ظهرًا.")
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
        reader = MorningAvailabilityReader(client)
        for property_obj in properties:
            try:
                zone = property_zone(property_obj)
                start = datetime.combine(data["date"], data["start_time"], zone)
                end = datetime.combine(data["date"], data["end_time"], zone)
                validate_window(start, end, zone=zone, allow_started=True)
            except MorningStayError:
                continue
            valid_windows += 1
            try:
                if active_stays(property_obj, start, end + timedelta(hours=3)).exists():
                    excluded.append(
                        {"name": str(property_obj), "reason": "يوجد طلب صباحي آخر متداخل للشقة."}
                    )
                    continue
                evidence = verify_window(
                    property_obj,
                    start,
                    end,
                    guests=data["guests"],
                    client=reader,
                    allow_started=True,
                )
            except MorningStayError as exc:
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
        label="ساعة الخروج — ١٢ ظهرًا كحد أقصى",
        initial=time(12),
        widget=forms.TimeInput(attrs={"type": "time", "max": "12:00"}),
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
        with translation.override("ar"):
            shared_phone = GuestDetailsForm()
            self.fields["guest_email"] = MorningBillingFields(customer=True).fields["guest_email"]
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
                billing_fields = MorningBillingForm(default_country_code="SA").fields
            for name, field in billing_fields.items():
                field.required = False
                self.fields[name] = field
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
            now = timezone.now().astimezone(ZONE) + timedelta(minutes=10)
            start = (
                now
                if now.hour < 12
                else datetime.combine(now.date() + timedelta(days=1), time(1), ZONE)
            )
            self.initial.update(
                request_id=uuid.uuid4(),
                date=start.date(),
                start_time=start.time().replace(second=0, microsecond=0),
            )

    def clean_guest_phone(self):
        try:
            country = self.data.get("guest_phone_country") or "SA"
            return normalize_morning_phone(self.cleaned_data["guest_phone"], country)
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
        if not self.stay and any(
            data.get(k) for k in self.fields if k.startswith("billing_") and k != "billing_country"
        ):
            billing = MorningBillingForm(data)
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
                        allow_started=True,
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
@morning_arabic_ui
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
            if request.POST.get("action") == "create_and_send":
                delivery = payment_messages.queue_initial(stay, dispatch_async=False)
                if delivery:
                    report_payment_send(request, payment_messages.send_link(delivery.pk))
                else:
                    messages.error(request, "حُفظ الطلب، لكن إرسال واتساب غير مفعّل؛ لم تُرسل رسالة.")
            return redirect("notifications:morning_detail", stay_id=stay.pk)
    return render(
        request,
        "admin/reservations/morning_create.html",
        context(request, title="تعديل الحجز الصباحي", form=form, editing=True),
    )


def context(request, **fields):
    from django.contrib import admin

    return {**admin.site.each_context(request), **address_context(request), **fields}


@operations_access_required
@morning_arabic_ui
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
@morning_arabic_ui
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
                billing_address=data.get("billing_address"),
                allow_started=True,
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
                summary="Morning stay saved for an explicit operations send.",
            )
            if request.POST.get("action") == "create_and_send":
                delivery = payment_messages.queue_initial(stay, dispatch_async=False)
                if delivery:
                    report_payment_send(request, payment_messages.send_link(delivery.pk))
                else:
                    messages.error(request, "حُفظ الطلب، لكن إرسال واتساب غير مفعّل؛ لم تُرسل رسالة.")
            return redirect("notifications:morning_detail", stay_id=stay.pk)
    return render(
        request,
        "admin/reservations/morning_create.html",
        context(request, title="إنشاء حجز صباحي", form=form),
    )


def report_payment_send(request, state):
    if state in {"accepted", "delivered", "read"}:
        messages.success(
            request, "قبلت خدمة واتساب رسالة الحجز ورابط الدفع. يمكنك مراجعة حالة الإرسال أدناه."
        )
    elif state in {"sending", "uncertain"}:
        messages.warning(
            request, "نتيجة إرسال واتساب غير محسومة؛ تحقق من وصول الرسالة قبل إعادة إرسالها."
        )
    else:
        messages.error(
            request,
            "لم تُرسل رسالة واتساب. تعذر التحقق من التوفر أو بيانات الطلب أو الاتصال؛ "
            "راجع حالة الإرسال ثم حاول مجددًا.",
        )


@operations_access_required
@morning_arabic_ui
def morning_detail(request, stay_id):
    stay = get_object_or_404(MorningStay.objects.select_related("property"), pk=stay_id)
    receipt_form = MorningReceiptForm(
        initial={"amount": stay.amount_sar, "received_at": timezone.localtime(timezone.now())}
    )
    if request.method == "POST" and request.POST.get("action") == "record_offline":
        receipt_form = MorningReceiptForm(request.POST)
        if receipt_form.is_valid():
            try:
                record_receipt(request=request, stay_id=stay.pk, **receipt_form.cleaned_data)
            except ValidationError as exc:
                receipt_form.add_error(None, exc)
            else:
                messages.success(
                    request, "تم توثيق السداد اليدوي وتحديث الحالة إلى مدفوع، دون تحصيل جديد."
                )
                return redirect("notifications:morning_detail", stay_id=stay.pk)
    elif request.method == "POST":
        action = request.POST.get("action")
        if action == "record_hostaway":
            try:
                identifier = record_in_hostaway(request=request, stay_id=stay.pk)
            except MorningStayError as exc:
                messages.error(request, str(exc))
            except HostawayError:
                messages.error(
                    request, "تعذر الاتصال بهوستاوي؛ راجع حالة الربط قبل إعادة المحاولة."
                )
            else:
                record_audit(
                    request=request,
                    action="morning_stay.hostaway_recorded",
                    object_type="MorningStay",
                    object_reference=str(stay.pk),
                    summary="Paid morning stay recorded as the previous calendar night.",
                    metadata={"hostaway_reservation_id": identifier},
                )
                messages.success(request, "سُجل الحجز الصباحي في هوستاوي دون تحصيل جديد.")
        elif action == "save_billing":
            billing = MorningBillingForm(request.POST)
            if billing.is_valid():
                from django.db import transaction

                with transaction.atomic():
                    current = MorningStay.objects.select_for_update().get(pk=stay.pk)
                    if (
                        current.status in {"open", "expired"}
                        and not current.attempts.filter(
                            status__in=["created", "pending", "unknown", "review", "succeeded"]
                        ).exists()
                    ):
                        current.billing_address = dict(billing.cleaned_data)
                        current.save(update_fields=["billing_address", "updated_at"])
                        messages.success(
                            request, "حُفظ عنوان الفوترة؛ يراجعه الضيف ويوافق بنفسه على الشروط."
                        )
                    else:
                        messages.error(
                            request, "لا يمكن تغيير العنوان أثناء دفع غير محسوم أو بعد السداد."
                        )
            else:
                return render(
                    request,
                    "admin/reservations/morning_billing.html",
                    context(request, title="عنوان فوترة الحجز الصباحي", form=billing, stay=stay),
                )
        elif action == "cancel":
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
        elif action == "send_payment_link":
            try:
                request_id = payment_messages.read_form_token(
                    request.POST.get("send_token", ""), stay, request.user
                )
                delivery = payment_messages.resend(stay.pk, request_id, dispatch_async=False)
            except MorningStayError as exc:
                messages.error(request, str(exc))
            except Exception:
                messages.error(request, "تعذر التحقق من التوفر الآن؛ لم يُرسل رابط جديد.")
            else:
                record_audit(
                    request=request,
                    action="morning_stay.payment_link_requested",
                    object_type="MorningStay",
                    object_reference=str(stay.pk),
                    summary="Operations requested the unpaid morning payment notice.",
                    metadata={"delivery_id": str(delivery.pk)},
                )
                report_payment_send(request, payment_messages.send_link(delivery.pk))
        elif action == "verify":
            attempt = stay.attempts.first()
            if attempt and attempt.provider_checkout_id:
                try:
                    verify_payment(attempt.pk)
                    messages.success(request, "تمت قراءة حالة الدفع من مزود الدفع.")
                except HyperPayError:
                    messages.error(request, "تعذر حسم الدفع الآن؛ لا تطلب دفعًا آخر قبل التحقق.")
        return redirect("notifications:morning_detail", stay_id=stay.pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    send_error = ""
    try:
        payment_messages.require_unpaid(stay)
    except MorningStayError as exc:
        send_error = str(exc)
    last_delivery = stay.payment_link_deliveries.first()
    return render(
        request,
        "admin/reservations/morning_detail.html",
        context(
            request,
            title="تفاصيل الحجز الصباحي",
            stay=stay,
            payment_url=public_url(stay)
            if settings.HYPERPAY_ENABLED and stay.status in {"open", "expired", "processing"}
            else "",
            attempts=stay.attempts.all(),
            expired=False,
            billing_form=MorningBillingForm(
                initial=stay.billing_address, default_country_code="SA"
            ),
            receipt_form=receipt_form,
            hostaway_booking=booking_state(stay),
            offline_receipt=getattr(stay, "offline_receipt", None),
            send_error=send_error,
            send_token=payment_messages.form_token(stay, request.user),
            payment_message=payment_messages.render_message(stay, preview=True)
            if not send_error
            else "",
            payment_delivery=last_delivery,
            payment_image="/static/images/morning/payment-" + stay.guest_language + ".png",
            payment_delivery_status=payment_messages.LABELS.get(last_delivery.state, "")
            if last_delivery
            else "لم يُطلب إرسال الرابط عبر واتساب بعد",
        ),
    )


@operations_access_required
def morning_address(request, kind):
    if kind == "phone":
        from .security import is_ip_rate_limited

        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if is_ip_rate_limited(request, scope="morning-phone", requests=60, window=300):
            return JsonResponse({"detail": "temporarily_unavailable"}, status=429)
        try:
            raw = request.POST.get("phone", "")
            if len(raw) > 32:
                raise InvalidPhoneNumber
            phone = normalize_morning_phone(raw, request.POST.get("country") or "SA")
        except InvalidPhoneNumber:
            return JsonResponse({"detail": "invalid_phone"}, status=400)
        region = phonenumbers.region_code_for_number(phonenumbers.parse(phone, None))
        return JsonResponse({"phone": phone, "country": region})
    if kind not in {"suggestions", "details"}:
        from django.http import Http404

        raise Http404
    return address_view(request, kind)
