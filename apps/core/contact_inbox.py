"""Owner-only contact reading, navigation and existing follow-up status controls."""

from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.accounts.access import require_operations_owner
from apps.notifications.models import EmailDelivery
from apps.notifications.services.audit import record_audit

from .models import ContactMessage

STATUS_LABELS = {
    "new": "جديدة",
    "in_progress": "قيد المتابعة",
    "closed": "مغلقة",
    "spam": "غير مرغوبة",
}
DELIVERY_LABELS = {
    "queued": "بانتظار الإرسال",
    "sending": "جارٍ الإرسال",
    "sent": "تم الإرسال",
    "failed": "تعذر الإرسال",
    "cancelled": "أُلغي الإرسال",
    "skipped": "تم تجاوز الإرسال",
    "disabled": "الإرسال غير مفعّل",
}


def _params(request):
    params = {}
    query = request.GET.get("q", "").strip()[:120]
    status = request.GET.get("status", "")
    if query:
        params["q"] = query
    if status in STATUS_LABELS:
        params["status"] = status
    page = request.GET.get("page", "")
    if page.isdecimal() and len(page) < 6:
        params["page"] = page
    return params


def _suffix(params):
    return "?" + urlencode(params) if params else ""


def _queryset(params):
    rows = ContactMessage.objects.all()
    if params.get("status"):
        rows = rows.filter(status=params["status"])
    if params.get("q"):
        query = params["q"]
        rows = rows.filter(
            Q(name__icontains=query)
            | Q(email__icontains=query)
            | Q(subject__icontains=query)
            | Q(message__icontains=query)
        )
    return rows.order_by("-created_at", "-pk")


def _detail_url(contact, params):
    return reverse("admin:core_contactmessage_change", args=[contact.pk]) + _suffix(params)


def _context(request, model_admin, params):
    return {
        **model_admin.admin_site.each_context(request),
        "title": "رسائل التواصل",
        "inbox_url": reverse("admin:core_contactmessage_changelist") + _suffix(params),
        "status_choices": list(STATUS_LABELS.items()),
        "selected_status": params.get("status", ""),
        "query": params.get("q", ""),
    }


def inbox(request, model_admin):
    require_operations_owner(request.user)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    params = _params(request)
    page = Paginator(_queryset(params), 25).get_page(params.get("page"))
    rows = [
        {
            "item": item,
            "status_label": STATUS_LABELS.get(item.status, "غير محددة"),
            "url": _detail_url(item, params),
        }
        for item in page.object_list
    ]
    context = _context(request, model_admin, params)
    context.update(
        {
            "rows": rows,
            "page_obj": page,
            "new_count": ContactMessage.objects.filter(status=ContactMessage.Status.NEW).count(),
            "previous_page_url": (
                _suffix({**params, "page": page.previous_page_number()})
                if page.has_previous()
                else ""
            ),
            "next_page_url": (
                _suffix({**params, "page": page.next_page_number()}) if page.has_next() else ""
            ),
        }
    )
    return render(request, "admin/core/contact_inbox.html", context)


def detail(request, model_admin, object_id):
    require_operations_owner(request.user)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    try:
        contact = get_object_or_404(ContactMessage, pk=object_id)
    except (TypeError, ValueError) as exc:
        raise Http404 from exc
    params = _params(request)
    if request.method == "POST":
        action = request.POST.get("action", "status" if "_save" in request.POST else "")
        if action != "status":
            return HttpResponseNotAllowed(["GET"])
        status = request.POST.get("status", "")
        if status in STATUS_LABELS:
            contact.status = status
            contact.save(update_fields=["status", "updated_at"])
            record_audit(
                request=request,
                action="contact.status_changed",
                object_type="ContactMessage",
                object_reference=str(contact.pk),
                summary="Owner updated contact follow-up status.",
                metadata={"status": status},
            )
            messages.success(request, "تم تحديث حالة الرسالة.")
            return redirect(_detail_url(contact, params))
        messages.error(request, "اختر حالة صحيحة للرسالة.")
    rows = _queryset(params)
    older = rows.filter(
        Q(created_at__lt=contact.created_at) | Q(created_at=contact.created_at, pk__lt=contact.pk)
    ).first()
    newer = (
        rows.filter(
            Q(created_at__gt=contact.created_at)
            | Q(created_at=contact.created_at, pk__gt=contact.pk)
        )
        .order_by("created_at", "pk")
        .first()
    )
    alert = EmailDelivery.objects.filter(
        idempotency_key=f"contact-admin-alert:{contact.pk}"
    ).first()
    context = _context(request, model_admin, params)
    context.update(
        {
            "contact": contact,
            "contact_status_label": STATUS_LABELS.get(contact.status, "غير محددة"),
            "previous_message_url": _detail_url(newer, params) if newer else "",
            "next_message_url": _detail_url(older, params) if older else "",
            "business_alert_recipient": settings.OPERATIONS_EMAIL,
            "alert_status": DELIVERY_LABELS.get(alert.status, "")
            if alert
            else "لا يوجد سجل تنبيه لهذه الرسالة",
        }
    )
    return render(request, "admin/core/contact_detail.html", context)
