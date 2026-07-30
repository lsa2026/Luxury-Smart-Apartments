from django.contrib import admin, messages
from django.http import HttpRequest

from .models import HostawayWebhookEvent, IntegrationSyncRun


@admin.register(IntegrationSyncRun)
class IntegrationSyncRunAdmin(admin.ModelAdmin):
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
        self.message_user(request, f"تمت إعادة {count} حدث إلى الطابور.")
