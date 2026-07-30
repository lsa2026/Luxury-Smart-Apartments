"""Admin-only operational views with public liveness/readiness probes elsewhere."""

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.integrations.models import HostawayWebhookEvent, IntegrationSyncRun
from apps.notifications.exports import report_rows, write_csv
from apps.notifications.health import readiness_status
from apps.notifications.models import EmailDelivery, Notification
from apps.notifications.reports import operations_report, report_period
from apps.notifications.services.audit import record_audit


def _require_operations_permission(request: HttpRequest) -> None:
    if not (
        request.user.is_superuser
        or request.user.has_perm("notifications.manage_notification_center")
    ):
        raise PermissionDenied


@staff_member_required
def notification_center(request: HttpRequest) -> HttpResponse:
    _require_operations_permission(request)
    queryset = Notification.objects.select_related("recipient_user").filter(
        status=Notification.Status.ACTIVE
    )
    notification_type = request.GET.get("type", "")
    severity = request.GET.get("severity", "")
    if notification_type in Notification.Type.values:
        queryset = queryset.filter(notification_type=notification_type)
    if severity in Notification.Severity.values:
        queryset = queryset.filter(severity=severity)
    from django.core.paginator import Paginator

    page = Paginator(queryset, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "admin/notifications/center.html",
        {
            "title": "مركز الإشعارات",
            "page_obj": page,
            "notification_types": Notification.Type.choices,
            "severities": Notification.Severity.choices,
            "unread_count": queryset.filter(is_read=False).count(),
        },
    )


@staff_member_required
@require_POST
def mark_notification_read(request: HttpRequest, notification_id: str) -> HttpResponse:
    _require_operations_permission(request)
    notification = get_object_or_404(Notification, pk=notification_id)
    if notification.audience_type == Notification.Audience.USER:
        if notification.recipient_user_id != request.user.pk:
            raise Http404
    notification.is_read = True
    notification.read_at = timezone.now()
    notification.save(update_fields=["is_read", "read_at"])
    return redirect("notifications:center")


@staff_member_required
@require_POST
def mark_all_notifications_read(request: HttpRequest) -> HttpResponse:
    _require_operations_permission(request)
    Notification.objects.filter(
        audience_type=Notification.Audience.ADMIN,
        status=Notification.Status.ACTIVE,
        is_read=False,
    ).update(is_read=True, read_at=timezone.now())
    return redirect("notifications:center")


@staff_member_required
def operations_dashboard(request: HttpRequest) -> HttpResponse:
    _require_operations_permission(request)
    start_date, end_date = report_period(
        request.GET.get("period", "30"),
        request.GET.get("start", ""),
        request.GET.get("end", ""),
    )
    return render(
        request,
        "admin/notifications/dashboard.html",
        {
            "title": "لوحة التقارير التشغيلية",
            "report": operations_report(start_date, end_date),
        },
    )


@staff_member_required
@require_POST
def export_report(request: HttpRequest, report_name: str) -> HttpResponse:
    _require_operations_permission(request)
    try:
        headers, rows = report_rows(report_name)
    except ValueError as exc:
        raise PermissionDenied from exc
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{report_name}.csv"'
    write_csv(response, headers, rows)
    record_audit(
        request=request,
        action="report.exported",
        object_type="OperationalReport",
        object_reference=report_name,
        summary="Operational report exported with PII hidden.",
        metadata={"report": report_name},
    )
    return response


@staff_member_required
def system_status(request: HttpRequest) -> HttpResponse:
    _require_operations_permission(request)
    ready, checks = readiness_status()
    status = {
        **checks,
        "ready": ready,
        "database_vendor": "postgresql" if connection.vendor == "postgresql" else "configured",
        "redis": "configured" if settings.REDIS_URL else "not_configured",
        "celery_worker": (
            "dispatch_enabled" if settings.CELERY_SYNC_DISPATCH_ENABLED else "not_enabled"
        ),
        "celery_beat": (
            "schedule_enabled" if settings.EMAIL_TASK_SCHEDULE_ENABLED else "not_enabled"
        ),
        "email_provider": ("enabled" if settings.EMAIL_DELIVERY_ENABLED else "disabled"),
        "hostaway_auth": (
            "configured"
            if settings.HOSTAWAY_ACCESS_TOKEN
            or (settings.HOSTAWAY_ACCOUNT_ID and settings.HOSTAWAY_API_SECRET)
            else "not_configured"
        ),
        "latest_sync": IntegrationSyncRun.objects.order_by("-started_at").first(),
        "latest_webhook": HostawayWebhookEvent.objects.order_by("-received_at").first(),
        "failed_email_tasks": EmailDelivery.objects.filter(
            status=EmailDelivery.Status.FAILED
        ).count(),
        "feature_flags": {
            "live_booking": settings.HOSTAWAY_LIVE_BOOKING_ENABLED,
            "live_modification": settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED,
            "live_cancellation": settings.HOSTAWAY_LIVE_CANCELLATION_ENABLED,
            "webhook_receiver": settings.HOSTAWAY_WEBHOOK_RECEIVER_ENABLED,
            "email_delivery": settings.EMAIL_DELIVERY_ENABLED,
        },
    }
    cache.set("operations:system-status:last-viewed", timezone.now().isoformat(), timeout=60)
    return render(
        request,
        "admin/notifications/system_status.html",
        {"title": "حالة النظام", "status": status},
    )
