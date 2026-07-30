from django.contrib import admin
from django.http import HttpRequest

from .models import IntegrationSyncRun


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
