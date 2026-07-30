from django.conf import settings
from django.contrib import admin, messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import path, reverse

from apps.notifications.services.audit import record_audit

from .health import get_integration_health
from .models import HostawayWebhookEvent, IntegrationSyncRun


@admin.register(IntegrationSyncRun)
class IntegrationSyncRunAdmin(admin.ModelAdmin):
    change_list_template = "admin/integrations/integrationsyncrun/change_list.html"
    list_display = (
        "sync_type",
        "status",
        "started_at",
        "completed_at",
        "fetched_count",
        "created_count",
        "updated_count",
        "failed_count",
        "dry_run",
    )
    list_filter = ("sync_type", "status", "dry_run")
    search_fields = ("error_summary",)
    readonly_fields = (
        "sync_type",
        "status",
        "started_at",
        "completed_at",
        "fetched_count",
        "created_count",
        "updated_count",
        "skipped_count",
        "failed_count",
        "error_summary",
        "triggered_by",
        "dry_run",
        "metadata",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: IntegrationSyncRun | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: IntegrationSyncRun | None = None,
    ) -> bool:
        return False

    def get_urls(self) -> list[object]:
        custom = [
            path(
                "health/",
                self.admin_site.admin_view(self.health_view),
                name="integrations_integration_health",
            )
        ]
        return custom + super().get_urls()

    def health_view(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_superuser:
            raise PermissionDenied
        if request.method == "POST":
            self._dispatch_sync(request)
            return redirect(reverse("admin:integrations_integration_health"))
        context = {
            **self.admin_site.each_context(request),
            "title": "حالة تكامل Hostaway",
            "health": get_integration_health(),
            "dispatch_enabled": settings.CELERY_SYNC_DISPATCH_ENABLED,
        }
        return render(request, "admin/integrations/integration_health.html", context)

    def _dispatch_sync(self, request: HttpRequest) -> None:
        action = request.POST.get("sync_action", "")
        command_map = {
            "properties": "python manage.py sync_hostaway_properties",
            "properties_dry_run": "python manage.py sync_hostaway_properties --dry-run",
            "reviews": "python manage.py sync_hostaway_reviews",
            "reviews_dry_run": "python manage.py sync_hostaway_reviews --dry-run",
        }
        command = command_map.get(action)
        if command is None:
            self.message_user(request, "إجراء مزامنة غير صالح.", messages.ERROR)
            return
        record_audit(
            request=request,
            action="hostaway.sync_requested",
            object_type="IntegrationSyncRun",
            object_reference=action,
            summary="A manual Hostaway sync action was requested.",
            metadata={"source": "admin", "dry_run": action.endswith("_dry_run")},
        )
        if not settings.CELERY_SYNC_DISPATCH_ENABLED:
            self.message_user(
                request,
                f"عامل المهام غير مفعّل. شغّل الأمر محليًا: {command}",
                messages.WARNING,
            )
            return
        from .tasks import sync_hostaway_properties_task, sync_hostaway_reviews_task

        dry_run = action.endswith("_dry_run")
        task = (
            sync_hostaway_properties_task
            if action.startswith("properties")
            else sync_hostaway_reviews_task
        )
        lock_name = "properties" if action.startswith("properties") else "reviews"
        if not cache.add(f"lsa:admin-dispatch:{lock_name}", "queued", timeout=60):
            self.message_user(request, "توجد مزامنة مماثلة أضيفت حديثًا.", messages.WARNING)
            return
        task.delay(dry_run=dry_run)
        self.message_user(request, "تمت إضافة المزامنة إلى الطابور.", messages.SUCCESS)


@admin.register(HostawayWebhookEvent)
class HostawayWebhookEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_type",
        "hostaway_object_id",
        "hostaway_reservation_id",
        "status",
        "received_at",
        "processed_at",
        "attempt_count",
        "error_code",
    )
    list_filter = ("event_type", "status", "received_at")
    search_fields = ("hostaway_object_id", "hostaway_reservation_id", "error_code")
    actions = ("requeue_failed",)
    fields = (
        "id",
        "external_event_id",
        "event_type",
        "hostaway_object_id",
        "hostaway_reservation_id",
        "deduplication_key",
        "body_hash",
        "payload_summary",
        "status",
        "attempt_count",
        "received_at",
        "processing_started_at",
        "processed_at",
        "next_retry_at",
        "error_code",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields

    @admin.display(description="البيانات المنقحة")
    def payload_summary(self, obj: HostawayWebhookEvent) -> str:
        keys = ", ".join(sorted(obj.sanitized_payload))
        return f"Allowed fields: {keys}" if keys else "No allowed fields"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: HostawayWebhookEvent | None = None,
    ) -> bool:
        return False

    @admin.action(description="إعادة الأحداث الفاشلة أو القابلة للمحاولة إلى الطابور")
    def requeue_failed(self, request: HttpRequest, queryset: object) -> None:
        if not request.user.is_superuser:
            self.message_user(request, "يتطلب الإجراء صلاحية عليا.", messages.ERROR)
            return
        count = queryset.filter(
            status__in=(
                HostawayWebhookEvent.Status.FAILED,
                HostawayWebhookEvent.Status.RETRYABLE,
            )
        ).update(
            status=HostawayWebhookEvent.Status.RECEIVED,
            next_retry_at=None,
            error_code="",
        )
        record_audit(
            request=request,
            action="webhook.requeued",
            object_type="HostawayWebhookEvent",
            object_reference="bulk",
            summary="Failed webhook events were queued for manual reprocessing.",
            metadata={"count": count},
        )
        self.message_user(request, f"تمت إعادة {count} حدث إلى الطابور.")
