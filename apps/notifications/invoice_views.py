"""Private owner-only invoice workflow, separate from reservation administration."""

import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.access import require_operations_owner
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import (
    HostawayAuthenticationError,
    HostawayConfigurationError,
    HostawayError,
    HostawayRateLimitError,
    HostawayTimeoutError,
)
from apps.notifications.invoice_forms import InvoiceLookupForm, InvoiceReviewForm, InvoiceSendForm
from apps.notifications.invoice_pdf import COPY, platform_name, render_invoice_pdf
from apps.notifications.models import GuestInvoice
from apps.notifications.services.audit import record_audit
from apps.notifications.services.invoice_email import invoice_filename
from apps.notifications.services.invoice_search import (
    find_invoice_bookings,
    read_search_token,
    search_token,
)
from apps.notifications.services.invoices import (
    issue_invoice,
    prepare_invoice,
    queue_invoice_delivery,
    refresh_invoice_presentation,
    snapshot_token,
)

logger = logging.getLogger(__name__)


def _audit(request, invoice, action, status):
    record_audit(
        request=request,
        action=action,
        object_type="guest_invoice",
        object_reference=str(invoice.pk),
        summary="إجراء على فاتورة ضيف",
        metadata={"status": status},
    )


@staff_member_required
@never_cache
@require_http_methods(["GET", "POST"])
def invoice_list(request):
    require_operations_owner(request.user)
    form = InvoiceLookupForm(request.POST if request.method == "POST" else None)
    candidates = None
    continuation = None
    phone = ""
    token = request.POST.get("phone_search_token", "") if request.method == "POST" else ""
    if token or (request.method == "POST" and form.is_valid()):
        try:
            selection = read_search_token(token, request.user) if token else None
            if selection:
                phone = selection["phone"]
                form = InvoiceLookupForm({"guest_phone": phone, "guest_phone_country": "SA"})
                form.is_valid()
                hostaway_id = selection.get("id")
                cursor = selection.get("cursor")
                if (hostaway_id is None) == (cursor is None):
                    raise ValidationError("نتائج البحث غير صالحة؛ أعد البحث برقم الجوال.")
            else:
                phone = form.cleaned_data.get("guest_phone", "")
                hostaway_id = form.cleaned_data.get("hostaway_id")
                cursor = None
            options = {"max_get_attempts": 1, "timeout": 20} if not hostaway_id else {}
            with HostawayClient(**options) as client:
                if hostaway_id:
                    invoice = prepare_invoice(
                        client=client,
                        hostaway_id=hostaway_id,
                        actor=request.user,
                        expected_phone=phone or None,
                    )
                    _audit(request, invoice, "invoice_lookup", "prepared")
                    return redirect("notifications:invoice_detail", invoice_id=invoice.pk)
                candidates, next_cursor = find_invoice_bookings(
                    client=client, phone=phone, after_id=cursor
                )
            for candidate in candidates:
                candidate["token"] = search_token(
                    actor=request.user, phone=phone, reservation_id=candidate["id"]
                )
            if next_cursor:
                continuation = search_token(actor=request.user, phone=phone, cursor=next_cursor)
        except HostawayError as exc:
            # No exception text, contact details, response body, tokens or URLs.
            logger.warning("Invoice lookup provider failure: %s", type(exc).__name__)
            if isinstance(exc, HostawayTimeoutError):
                message = (
                    "استغرق Hostaway وقتًا أطول من المعتاد؛ أعد البحث. "
                    "رقم الجوال صالح ولم يصدر أي طلب إرسال."
                )
            elif isinstance(exc, HostawayRateLimitError):
                message = "وصل الاتصال بـHostaway إلى حد الطلبات؛ انتظر قليلًا ثم أعد البحث."
            elif isinstance(exc, HostawayAuthenticationError | HostawayConfigurationError):
                message = (
                    "تعذر اتصال الموقع بحساب Hostaway؛ يلزم مراجعة إعدادات الربط، "
                    "وليس تغيير رقم الجوال."
                )
            else:
                message = (
                    "تعذر قراءة الحجوزات من Hostaway. "
                    "لم يتم تغيير أي حجز أو إرسال فاتورة؛ أعد المحاولة."
                )
            form.add_error(None, message)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "admin/notifications/invoice_list.html",
        {
            "title": "فواتير الضيوف",
            "form": form,
            "candidates": candidates,
            "phone_searched": phone,
            "continuation": continuation,
            "invoices": GuestInvoice.objects.defer("pdf_content").order_by("-created_at")[:50],
        },
    )


def _detail(request, invoice, form=None, send_forms=None):
    data = invoice.snapshot
    delivery_forms = {
        channel: InvoiceSendForm(
            initial={
                "channel": channel,
                "recipient": data.get(key, ""),
                "guest_phone_country": "SA",
            },
            auto_id=f"invoice-{channel}-%s",
        )
        for channel, key in (("email", "guest_email"), ("whatsapp", "guest_phone"))
    }
    delivery_forms.update(send_forms or {})
    return render(
        request,
        "admin/notifications/invoice_detail.html",
        {
            "title": "فاتورة الضيف",
            "invoice": invoice,
            "data": data,
            "form": form
            if form is not None
            else InvoiceReviewForm(
                initial={
                    **data,
                    "language": invoice.language,
                    "snapshot_token": snapshot_token(invoice),
                }
            ),
            "copy": COPY[invoice.language],
            "property_name": data["property_names"][invoice.language],
            "platform_name": platform_name(data["platform"]),
            "presentation": invoice.presentations.defer("pdf_content").first(),
            "deliveries": invoice.deliveries.all()[:20],
            "email_send_form": delivery_forms["email"],
            "whatsapp_send_form": delivery_forms["whatsapp"],
        },
    )


@staff_member_required
@never_cache
@require_GET
def invoice_detail(request, invoice_id):
    require_operations_owner(request.user)
    return _detail(
        request, get_object_or_404(GuestInvoice.objects.defer("pdf_content"), pk=invoice_id)
    )


@staff_member_required
@never_cache
@require_POST
def invoice_issue(request, invoice_id):
    require_operations_owner(request.user)
    invoice = get_object_or_404(GuestInvoice, pk=invoice_id)
    if not invoice.issued_at:
        form = InvoiceReviewForm(request.POST)
        if not form.is_valid():
            return _detail(request, invoice, form)
        # Never allow replacing the platform reference with the internal Hostaway ID.
        if form.cleaned_data["platform_reference"] == str(invoice.hostaway_reservation_id):
            form.add_error(
                "platform_reference", "أدخل مرجع المنصة الذي يعرفه الضيف، وليس رقم Hostaway."
            )
            return _detail(request, invoice, form)
        try:
            invoice = issue_invoice(invoice.pk, form.cleaned_data)
        except ValidationError as exc:
            invoice.refresh_from_db()
            messages.error(request, " ".join(exc.messages))
            return redirect("notifications:invoice_detail", invoice_id=invoice.pk)
        _audit(request, invoice, "invoice_issue", "issued")
        messages.success(request, "صدرت الفاتورة وحُفظت نسخة ثابتة؛ يمكنك تنزيلها أو إرسالها.")
    return redirect("notifications:invoice_detail", invoice_id=invoice.pk)


@staff_member_required
@never_cache
@require_GET
def invoice_pdf(request, invoice_id):
    require_operations_owner(request.user)
    invoice = get_object_or_404(GuestInvoice, pk=invoice_id)
    presentation = invoice.presentations.first() if invoice.issued_at else None
    if invoice.issued_at:
        content = bytes(presentation.pdf_content if presentation else invoice.pdf_content)
    else:
        content = render_invoice_pdf(invoice)
    if request.GET.get("original") == "1" and invoice.issued_at:
        content = bytes(invoice.pdf_content)
    response = HttpResponse(content, content_type="application/pdf")
    filename = invoice_filename(invoice)
    disposition = "attachment" if request.GET.get("download") == "1" else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'self'"
    return response


@staff_member_required
@never_cache
@require_POST
def invoice_refresh_design(request, invoice_id):
    require_operations_owner(request.user)
    invoice = get_object_or_404(GuestInvoice, pk=invoice_id)
    try:
        refresh_invoice_presentation(invoice.pk, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        _audit(request, invoice, "invoice_design_refresh", "retained_original")
        messages.success(
            request,
            "حُفظ التصميم الجديد مع الاحتفاظ بالأصل، "
            "دون تغيير بيانات الحجز أو المبلغ أو إرسال رسالة.",
        )
    return redirect("notifications:invoice_detail", invoice_id=invoice.pk)


@staff_member_required
@never_cache
@require_POST
def invoice_send(request, invoice_id):
    require_operations_owner(request.user)
    invoice = get_object_or_404(GuestInvoice, pk=invoice_id)
    channel = request.POST.get("channel", "")
    if channel not in {"email", "whatsapp"}:
        messages.error(request, "اختر الإرسال عبر البريد الإلكتروني أو واتساب.")
        return redirect("notifications:invoice_detail", invoice_id=invoice.pk)
    form = InvoiceSendForm(request.POST, auto_id=f"invoice-{channel}-%s")
    if form.is_valid():
        try:
            delivery = queue_invoice_delivery(
                invoice=invoice,
                channel=form.cleaned_data["channel"],
                recipient=form.cleaned_data["recipient"],
                actor=request.user,
            )
            _audit(request, invoice, "invoice_send_requested", delivery.status)
            messages.info(
                request, "تم تسجيل طلب الإرسال. راجع سجل الإرسال أدناه؛ الطلب المكرر لن يُرسل مرتين."
            )
        except ValidationError as exc:
            form.add_error(None, exc)
            return _detail(request, invoice, send_forms={channel: form})
    else:
        return _detail(request, invoice, send_forms={channel: form})
    return redirect("notifications:invoice_detail", invoice_id=invoice.pk)
