"""Operations-only selector across Hostaway channels and extension review."""

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core import signing
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone, translation

from apps.accounts.access import operations_access_required
from apps.integrations.hostaway.client import HostawayClient
from apps.notifications.services.audit import record_audit
from apps.payments.morning_validation import MorningBillingFields, validate_value

from .manual_bookings import (
    create_manual_booking_in_hostaway,
    finalize_manual_booking_draft,
    recheck_manual_booking_draft,
)
from .models import StayExtension
from .morning_billing import address_context
from .operations_forms import ManualBookingFinalizeForm
from .stay_extension_communications import context
from .stay_extensions import (
    UnsupportedStaySource,
    digest,
    eligible_source,
    guest_initial,
    read_source,
    refresh_chain,
    start_extension,
    unsupported_source_reason,
)

TICKET_SALT = "reservations.extension-source.v1"
CURSOR_SALT = "reservations.extension-selector.v1"


class ExtensionGuestForm(ManualBookingFinalizeForm):
    final_total_price = forms.DecimalField(
        label="إجمالي السعر النهائي للتمديد",
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        approved = MorningBillingFields(customer=True)
        for name in ("guest_first_name", "guest_last_name", "guest_email"):
            self.fields[name] = approved.fields[name]

    def clean(self):
        cleaned = super().clean()
        for name in ("guest_first_name", "guest_last_name", "guest_email"):
            if name in cleaned:
                try:
                    cleaned[name] = validate_value(name, cleaned[name])
                except forms.ValidationError as exc:
                    self.add_error(name, exc)
        return cleaned


class ExtensionDatesForm(ExtensionGuestForm):
    check_out = forms.DateField(
        label="تاريخ الخروج الجديد", widget=forms.DateInput(attrs={"type": "date"})
    )
    guests = forms.IntegerField(label="عدد الضيوف", min_value=1, max_value=20)
    source_ticket = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, raw=None, **kwargs):
        raw = raw or {}
        draft = SimpleNamespace(
            billing_address={},
            final_total_price=Decimal("0"),
            currency=str(raw.get("currency") or "SAR"),
            **guest_initial(raw),
        )
        super().__init__(*args, draft=draft, **kwargs)
        self.fields.pop("final_total_price")


def _source_ticket(request, snapshot):
    return signing.dumps(
        {"user": str(request.user.pk), "id": snapshot["id"], "fingerprint": digest(snapshot)},
        salt=TICKET_SALT,
    )


@operations_access_required
@translation.override("ar")
def source_list(request):
    rows, selected, error, next_url = [], None, "", ""
    query = request.GET.get("q", "").strip()[:100]
    channel = request.GET.get("channel", "").strip()[:80]
    listing = request.GET.get("listing", "").strip()[:20]
    dates_form = None
    try:
        if request.method == "POST":
            dates_form = ExtensionDatesForm(request.POST)
            if dates_form.is_valid():
                ticket = signing.loads(
                    dates_form.cleaned_data["source_ticket"], salt=TICKET_SALT, max_age=900
                )
                if ticket["user"] != str(request.user.pk):
                    raise ValueError("source_ticket_invalid")
                raw = read_source(int(ticket["id"]))
                snapshot, property_obj = eligible_source(raw)
                if digest(snapshot) != ticket["fingerprint"]:
                    raise ValueError("source_changed")
                if (
                    property_obj.person_capacity
                    and dates_form.cleaned_data["guests"] > property_obj.person_capacity
                ):
                    raise ValueError("capacity_exceeded")
                extension = start_extension(
                    raw=raw,
                    check_out=dates_form.cleaned_data["check_out"],
                    guests=dates_form.cleaned_data["guests"],
                    actor=request.user,
                    guest_data=dates_form.cleaned_data,
                )
                record_audit(
                    request=request,
                    action="stay_extension.draft_created",
                    object_type="StayExtension",
                    object_reference=str(extension.pk),
                    summary="فحص التوفر وتجهيز تمديد مستقل؛ لم يُنشأ حجز خارجي.",
                )
                return redirect("notifications:stay_extension_detail", extension_id=extension.pk)
            error = "راجع تاريخ الخروج وعدد الضيوف ثم أعد فحص التوفر."
        selected_id = request.GET.get("source", "")
        if selected_id:
            if not selected_id.isascii() or not selected_id.isdigit() or len(selected_id) > 18:
                raise ValueError("source_invalid")
            raw = read_source(int(selected_id))
            snapshot, property_obj = eligible_source(raw)
            selected = {"snapshot": snapshot, "property": property_obj, "guest": guest_initial(raw)}
            if dates_form is None:
                dates_form = ExtensionDatesForm(
                    raw=raw,
                    initial={
                        "source_ticket": _source_ticket(request, snapshot),
                        "guests": min(snapshot["guests"], 20),
                        "check_out": date.fromisoformat(snapshot["departure_date"])
                        + timedelta(days=1),
                    },
                )
            dates_form.fields["check_out"].widget.attrs["min"] = (
                date.fromisoformat(snapshot["departure_date"]) + timedelta(days=1)
            ).isoformat()
        else:
            binding = digest(
                {
                    "q": query,
                    "channel": channel,
                    "listing": listing,
                    "day": timezone.localdate().isoformat(),
                    "user": str(request.user.pk),
                }
            )
            after_id, seen = None, []
            if request.GET.get("cursor"):
                cursor = signing.loads(request.GET["cursor"], salt=CURSOR_SALT, max_age=3600)
                if cursor["binding"] != binding:
                    raise ValueError("cursor_changed")
                after_id, seen = cursor["after"], cursor["seen"]
            with HostawayClient() as client:
                if query.isascii() and query.isdigit() and not after_id:
                    raw_rows = [client.get_reservation_invoice_data(int(query))["result"]]
                    exact = True
                else:
                    raw_rows = client.guest_stay_page(
                        timezone.localdate() - timedelta(days=1),
                        timezone.localdate() + timedelta(days=1),
                        after_id=after_id,
                    )
                    exact = False
            ids = [raw.get("id") for raw in raw_rows]
            if any(type(value) is not int or value <= 0 for value in ids) or len(ids) != len(
                set(ids)
            ):
                raise ValueError("source_page_invalid")
            if raw_rows and not exact:
                if ids[-1] in seen or ids[-1] == after_id:
                    raise ValueError("cursor_did_not_advance")
                cursor = signing.dumps(
                    {"after": ids[-1], "seen": seen + [ids[-1]], "binding": binding},
                    salt=CURSOR_SALT,
                    compress=True,
                )
                next_url = "?" + urlencode(
                    {"q": query, "channel": channel, "listing": listing, "cursor": cursor}
                )
            for raw in raw_rows:
                try:
                    snapshot, property_obj = eligible_source(raw, for_display=True)
                except ValueError:
                    continue
                guest = guest_initial(raw)
                name = f"{guest['guest_first_name']} {guest['guest_last_name']}".strip()
                if query and not exact and query.casefold() not in name.casefold():
                    continue
                channel_aliases = {
                    "بوكنق": "booking",
                    "بوكينج": "booking",
                    "إير بي إن بي": "airbnb",
                    "اير بي ان بي": "airbnb",
                    "موقعنا": "direct",
                }
                channel_match = channel_aliases.get(channel.casefold(), channel.casefold())
                if channel_match and channel_match not in snapshot["channel_name"].casefold():
                    continue
                if listing and listing != str(snapshot["listing_map_id"]):
                    continue
                rows.append(
                    {
                        "snapshot": snapshot,
                        "property": property_obj,
                        "name": name,
                        "blocked_reason": unsupported_source_reason(raw),
                    }
                )
    except UnsupportedStaySource as exc:
        error = str(exc)
        next_url = ""
    except Exception:
        # No raw provider data, contact, or signed ticket enters logs/errors.
        error = "تعذر إكمال القراءة أو تغير الحجز. أعد البحث؛ لم يُنشأ حجز أو دفع أو رسالة."
        next_url = ""
    return render(
        request,
        "admin/reservations/stay_extension_sources.html",
        {
            "title": "تمديد الحجز",
            "rows": rows,
            "selected": selected,
            "form": dates_form,
            "error": error,
            "next_url": next_url,
            "query": query,
            "channel": channel,
            "listing": listing,
        },
    )


@operations_access_required
@translation.override("ar")
def detail(request, extension_id):
    extension = get_object_or_404(
        StayExtension.objects.select_related(
            "draft__quote", "chain__property", "reservation__booking_intent"
        ),
        pk=extension_id,
    )
    draft = extension.draft
    form = ExtensionGuestForm(draft=draft, require_address=True)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "recheck" and extension.reservation_id is None:
            result = recheck_manual_booking_draft(draft_id=draft.pk, actor=request.user)
            if result.code == "rechecked":
                messages.success(
                    request, "حُدّث التوفر والسعر. راجع بيانات الضيف والسعر النهائي مجددًا."
                )
                return redirect("notifications:stay_extension_detail", extension_id=extension.pk)
            messages.error(request, "تعذر تحديث التوفر والسعر. لم يُنشأ حجز جديد.")
        elif action == "create":
            if request.POST.get("confirm_independent_booking") != "yes":
                messages.error(request, "أكد مراجعة الحجز المستقل قبل إنشائه.")
            else:
                result = create_manual_booking_in_hostaway(draft_id=draft.pk)
                if result.code in {"created", "already_created"} and result.reservation:
                    from apps.payments.payment_link_tasks import queue_delivery
                    from apps.payments.payment_links import issue_request

                    try:
                        payment_request = issue_request(result.reservation.pk)
                        delivery = queue_delivery(
                            payment_request.pk
                        )  # Stable initial send, never implicit resend.
                        messages.success(
                            request,
                            "حجز التمديد محفوظ وبانتظار الدفع. حالة طلب الإرسال: "
                            + {
                                "queued": "بانتظار الإرسال",
                                "sending": "جارٍ الإرسال",
                                "sent": "قبله مزود الرسائل؛ لم يُثبت وصول الجوال",
                                "failed": "تحتاج مراجعة",
                                "unknown": "غير مؤكدة وتحتاج مراجعة",
                            }.get(delivery.status, "تحتاج مراجعة"),
                        )
                    except Exception:
                        messages.warning(
                            request,
                            "حجز التمديد محفوظ، لكن تجهيز رابط السداد يحتاج مراجعة. "
                            "لم يُنشأ حجز مكرر.",
                        )
                    record_audit(
                        request=request,
                        action="stay_extension.created",
                        object_type="StayExtension",
                        object_reference=str(extension.pk),
                        summary="حجز مباشر مستقل؛ الأصل ودفعاته محفوظة دون تعديل.",
                    )
                    return redirect("notifications:booking_list")
                messages.error(
                    request,
                    "لم يتأكد إنشاء التمديد. راجع التوفر وملكية الأتمتة وسجل العملية؛ "
                    "لا تعاود إنشاء حجز بديل عند غموض النتيجة.",
                )
        elif action == "save" and extension.reservation_id is None:
            form = ExtensionGuestForm(request.POST, draft=draft, require_address=True)
            if form.is_valid():
                result = finalize_manual_booking_draft(
                    draft_id=draft.pk,
                    guest_data=form.cleaned_data,
                    final_total_price=form.cleaned_data["final_total_price"],
                )
                if result.code == "ready_for_payment":
                    refresh_chain(extension.chain_id)
                    return redirect(
                        "notifications:stay_extension_detail", extension_id=extension.pk
                    )
                form.add_error(None, "انتهت صلاحية السعر أو تعذر اعتماده. أعد فحص التوفر أولًا.")
        else:
            messages.error(request, "لا يمكن تنفيذ هذا الإجراء في الحالة الحالية.")
    extension.refresh_from_db()
    draft.refresh_from_db()
    return render(
        request,
        "admin/reservations/stay_extension_detail.html",
        {
            "title": "مراجعة تمديد الحجز",
            "extension": extension,
            "draft": draft,
            "form": form,
            "chain": context(extension),
            **address_context(request),
        },
    )
