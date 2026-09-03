from django.contrib import admin, messages
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import AuditLog, EmailDelivery, Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "title_ar",
        "notification_type",
        "severity",
        "audience_type",
        "is_read",
        "created_at",
    )
    list_filter = (
        "notification_type",
        "severity",
        "audience_type",
        "status",
        "is_read",
        "created_at",
    )
    search_fields = ("title_ar", "title_en", "message_ar", "message_en")
    readonly_fields = (
        "id",
        "notification_type",
        "audience_type",
        "recipient_user",
        "title_ar",
        "title_en",
        "message_ar",
        "message_en",
        "action_url",
        "severity",
        "related_object_type",
        "related_object_reference",
        "idempotency_key",
        "created_at",
        "expires_at",
    )
    actions = ("mark_read", "mark_unread")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.action(description=_("Mark notifications as read"))
    def mark_read(self, request: HttpRequest, queryset: object) -> None:
        count = queryset.update(is_read=True, read_at=timezone.now())
        self.message_user(
            request,
            _("Updated %(count)d notification(s).") % {"count": count},
            messages.SUCCESS,
        )

    @admin.action(description=_("Mark notifications as unread"))
    def mark_unread(self, request: HttpRequest, queryset: object) -> None:
        count = queryset.update(is_read=False, read_at=None)
        self.message_user(
            request,
            _("Updated %(count)d notification(s).") % {"count": count},
            messages.SUCCESS,
        )


@admin.register(EmailDelivery)
class EmailDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "message_type",
        "recipient_masked",
        "language",
        "status",
        "provider",
        "attempt_count",
        "queued_at",
        "sent_at",
    )
    list_filter = ("message_type", "language", "status", "provider", "created_at")
    search_fields = ("recipient_masked", "idempotency_key", "last_error_code")
    readonly_fields = tuple(field.name for field in EmailDelivery._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: EmailDelivery | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: EmailDelivery | None = None,
    ) -> bool:
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "action",
        "object_type",
        "object_reference",
        "actor_user",
        "created_at",
    )
    list_filter = ("action", "object_type", "created_at")
    search_fields = ("action", "object_type", "object_reference", "summary")
    readonly_fields = tuple(field.name for field in AuditLog._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: AuditLog | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: AuditLog | None = None,
    ) -> bool:
        return False
