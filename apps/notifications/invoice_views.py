"""Private owner-only invoice workflow, separate from reservation administration."""

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.access import require_operations_owner
from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayError
from apps.notifications.invoice_forms import InvoiceLookupForm, InvoiceReviewForm, InvoiceSendForm
from apps.notifications.invoice_pdf import COPY, render_invoice_pdf
from apps.notifications.models import GuestInvoice
from apps.notifications.services.audit import record_audit
from apps.notifications.services.invoice_search import (
    find_invoice_bookings,
    read_search_token,
    search_token,
)
from apps.notifications.services.invoices import (
    issue_invoice,
    prepare_invoice,
    queue_invoice_delivery,
    snapshot_token,
)


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
            options = {"max_get_attempts": 1, "timeout": 8} if not hostaway_id else {}
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
        except HostawayError:
            form.add_error(
                None, "تعذر قراءة الحجوزات من Hostaway؛ تحقق من الرقم والاتصال ثم حاول مجددًا."
            )
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


def _detail(request, invoice, form=None):
    data = invoice.snapshot
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
            "deliveries": invoice.deliveries.all()[:20],
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
    content = bytes(invoice.pdf_content) if invoice.issued_at else render_invoice_pdf(invoice)
    response = HttpResponse(content, content_type="application/pdf")
    filename = f"{invoice.number or 'Luxury-Living-preview'}.pdf"
    disposition = "attachment" if request.GET.get("download") == "1" else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'self'"
    return response


@staff_member_required
@never_cache
@require_POST
def invoice_send(request, invoice_id):
    require_operations_owner(request.user)
    invoice = get_object_or_404(GuestInvoice, pk=invoice_id)
    form = InvoiceSendForm(request.POST)
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
            messages.error(request, " ".join(exc.messages))
    else:
        messages.error(request, "تحقق من عنوان المستلم أو رقمه، وأكّد الموافقة على الإرسال.")
    return redirect("notifications:invoice_detail", invoice_id=invoice.pk)
