"""Owner-only, local contact workspace with explicitly submitted email replies."""

import hashlib
import uuid
from datetime import timedelta
from email.utils import parseaddr
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from apps.accounts.access import require_operations_owner
from apps.notifications.models import EmailDelivery
from apps.notifications.services.audit import record_audit
from apps.notifications.services.email import queue_email, recipient_hmac

from .models import ContactAIDraft, ContactMessage, ContactReply

STATUS_LABELS = {
    "new": "جديدة",
    "read": "مقروء",
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
SALT = "contact-owner-reply.v1"


class ReplyForm(forms.Form):
    body = forms.CharField(
        label="نص الرد",
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 7, "dir": "auto", "placeholder": "اكتب ردك هنا…"}),
        error_messages={"required": "اكتب نص الرد أولًا.", "max_length": "الحد الأقصى ٥٠٠٠ حرف."},
    )
    language = forms.ChoiceField(
        label="لغة البريد", choices=(("ar", "العربية"), ("en", "English"), ("fr", "Français"))
    )
    request_token = forms.CharField(widget=forms.HiddenInput)


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


def _token(contact, user):
    return signing.dumps(
        {
            "contact": contact.pk,
            "user": user.pk,
            "recipient": recipient_hmac(contact.email),
            "id": str(uuid.uuid4()),
        },
        salt=SALT,
    )


def _read_token(token, contact, user):
    try:
        data = signing.loads(token, salt=SALT, max_age=86400)
        if (
            data["contact"] != contact.pk
            or data["user"] != user.pk
            or data["recipient"] != recipient_hmac(contact.email)
        ):
            raise ValueError
        return uuid.UUID(data["id"])
    except (signing.BadSignature, KeyError, ValueError, TypeError) as exc:
        raise ValidationError("انتهت صلاحية نموذج الرد أو تغيّرت بياناته. أعد فتح الرسالة.") from exc


def _queue_reply(request, contact, values):
    with transaction.atomic():
        contact = ContactMessage.objects.select_for_update().get(pk=contact.pk)
        reply_id = _read_token(values["request_token"], contact, request.user)
        existing = ContactReply.objects.filter(pk=reply_id).first()
        if existing:
            if (
                existing.contact_id != contact.pk
                or existing.created_by_id != request.user.pk
                or existing.body != values["body"]
                or existing.language != values["language"]
            ):
                raise ValidationError("هذا النموذج استُخدم لرد آخر. أعد فتح الرسالة لكتابة رد جديد.")
            return existing, False
        if not settings.EMAIL_DELIVERY_ENABLED:
            raise ValidationError("إرسال البريد غير مفعّل حاليًا. لم يُرسل الرد.")
        reply = ContactReply.objects.create(
            pk=reply_id,
            contact=contact,
            recipient_email=contact.email,
            body=values["body"],
            language=values["language"],
            created_by=request.user,
        )
        delivery = queue_email(
            message_type="contact_reply",
            recipient=reply.recipient_email,
            recipient_source="contact_reply",
            recipient_reference=str(reply.pk),
            language=reply.language,
            idempotency_key=f"contact-reply:{reply.pk}",
        )
        prefix = {"ar": "رد على", "en": "Re", "fr": "Re"}[reply.language]
        subject = strip_tags(contact.subject).replace("\r", " ").replace("\n", " ").strip()
        delivery.subject = f"{prefix}: {subject}"[:250]
        delivery.save(update_fields=["subject", "updated_at"])
        reply.delivery = delivery
        reply.save(update_fields=["delivery"])
        if contact.status in {ContactMessage.Status.NEW, ContactMessage.Status.READ}:
            contact.status = ContactMessage.Status.IN_PROGRESS
            contact.save(update_fields=["status", "updated_at"])
        record_audit(
            request=request,
            action="contact.reply_queued",
            object_type="ContactReply",
            object_reference=str(reply.pk),
            summary="Owner requested a contact email reply.",
            metadata={"status": delivery.status},
        )
        return reply, True


def detail(request, model_admin, object_id):
    require_operations_owner(request.user)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    try:
        contact = get_object_or_404(ContactMessage, pk=object_id)
    except (TypeError, ValueError) as exc:
        raise Http404 from exc
    if request.method == "GET":
        changed = ContactMessage.objects.filter(pk=contact.pk, status="new").update(status="read")
        if changed:
            contact.status = "read"
    contact.ai_drafts.filter(
        status="pending", created_at__lt=timezone.now() - timedelta(minutes=3)
    ).update(status="failed")
    draft = contact.ai_drafts.first()
    params = _params(request)
    form = ReplyForm(
        initial={
            "language": contact.language,
            "request_token": _token(contact, request.user),
            "body": draft.body if draft and draft.status == "ready" else "",
        }
    )
    preview = ""
    if request.method == "POST":
        action = request.POST.get("action", "status" if "_save" in request.POST else "")
        if action == "draft":
            try:
                identity = _read_token(request.POST.get("request_token", ""), contact, request.user)
                language = request.POST.get("language", contact.language)
                if language not in {"ar", "en", "fr"}:
                    raise ValidationError("اختر لغة صحيحة.")
                ContactAIDraft.objects.get_or_create(
                    pk=identity,
                    defaults={
                        "contact": contact,
                        "requested_by": request.user,
                        "language": language,
                        "instructions": request.POST.get("instructions", "").strip()[:1000],
                        "source_hash": hashlib.sha256(
                            (contact.subject + "\n" + contact.message).encode()
                        ).hexdigest(),
                    },
                )
                messages.success(request, "طُلبت مسودة من سما؛ لم يُرسل أي بريد.")
                return redirect(_detail_url(contact, params) + "#reply-form")
            except ValidationError as exc:
                form.add_error(None, exc)
        elif action == "status":
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
        elif action in {"preview", "send"}:
            form = ReplyForm(request.POST)
            if form.is_valid():
                try:
                    _read_token(form.cleaned_data["request_token"], contact, request.user)
                    if action == "preview":
                        preview = form.cleaned_data["body"]
                    else:
                        _, created = _queue_reply(request, contact, form.cleaned_data)
                        messages.success(
                            request,
                            "أُدرج الرد للإرسال بالبريد."
                            if created
                            else "الرد مسجّل مسبقًا؛ لم يُكرر الإرسال.",
                        )
                        return redirect(_detail_url(contact, params) + "#reply-history")
                except ValidationError as exc:
                    form.add_error(None, exc)
                except DatabaseError:
                    form.add_error(None, "تعذر تسجيل الرد الآن. لم يُنشأ إرسال جديد؛ حاول لاحقًا.")
        else:
            return HttpResponseNotAllowed(["GET"])
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
    replies = [
        {
            "item": reply,
            "status_label": DELIVERY_LABELS.get(reply.delivery.status, "الحالة غير متاحة")
            if reply.delivery
            else "سجل الإرسال غير متاح",
        }
        for reply in contact.replies.select_related("delivery").all()
    ]
    alert = EmailDelivery.objects.filter(
        idempotency_key=f"contact-admin-alert:{contact.pk}"
    ).first()
    context = _context(request, model_admin, params)
    context.update(
        {
            "ai_draft": draft,
            "contact": contact,
            "contact_status_label": STATUS_LABELS.get(contact.status, "غير محددة"),
            "reply_form": form,
            "preview_body": preview,
            "replies": replies,
            "previous_message_url": _detail_url(newer, params) if newer else "",
            "next_message_url": _detail_url(older, params) if older else "",
            "email_enabled": settings.EMAIL_DELIVERY_ENABLED,
            "business_sender": parseaddr(settings.DEFAULT_FROM_EMAIL)[1],
            "business_alert_recipient": settings.OPERATIONS_EMAIL,
            "business_reply_to": settings.EMAIL_REPLY_TO
            or parseaddr(settings.DEFAULT_FROM_EMAIL)[1],
            "alert_status": DELIVERY_LABELS.get(alert.status, "")
            if alert
            else "لا يوجد سجل تنبيه لهذه الرسالة",
        }
    )
    return render(request, "admin/core/contact_detail.html", context)
