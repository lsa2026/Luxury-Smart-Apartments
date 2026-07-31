from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.notifications.services.audit import record_audit

from .models import Review


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = (
        "guest_name",
        "property",
        "rating",
        "status",
        "channel_id",
        "is_visible",
        "is_featured",
    )
    list_filter = ("property", "rating", "status", "is_visible", "is_featured")
    search_fields = (
        "guest_name",
        "public_review",
        "public_review_ar",
        "public_review_en",
        "public_review_fr",
    )
    list_select_related = ("property",)
    actions = ("make_visible", "make_hidden", "make_featured")
    readonly_fields = (
        "hostaway_review_id",
        "property",
        "hostaway_listing_map_id",
        "hostaway_reservation_id",
        "external_review_id",
        "channel_id",
        "review_type",
        "status",
        "guest_name",
        "rating",
        "public_review",
        "reviewee_response",
        "arrival_date",
        "departure_date",
        "source_updated_at",
        "synced_at",
        "created_at",
        "updated_at",
    )

    @admin.action(description="إظهار المراجعات المحددة")
    def make_visible(self, request: HttpRequest, queryset: QuerySet[Review]) -> None:
        updated = queryset.update(is_visible=True)
        record_audit(
            request=request,
            action="review.visibility_changed",
            object_type="Review",
            object_reference="bulk",
            summary="Reviews were made visible.",
            metadata={"count": updated, "status": "visible"},
        )
        self.message_user(request, f"تم إظهار {updated} مراجعة.", messages.SUCCESS)

    @admin.action(description="إخفاء المراجعات المحددة")
    def make_hidden(self, request: HttpRequest, queryset: QuerySet[Review]) -> None:
        updated = queryset.update(is_visible=False)
        record_audit(
            request=request,
            action="review.visibility_changed",
            object_type="Review",
            object_reference="bulk",
            summary="Reviews were hidden.",
            metadata={"count": updated, "status": "hidden"},
        )
        self.message_user(request, f"تم إخفاء {updated} مراجعة.", messages.SUCCESS)

    @admin.action(description="تمييز المراجعات المحددة")
    def make_featured(self, request: HttpRequest, queryset: QuerySet[Review]) -> None:
        updated = queryset.update(is_featured=True)
        record_audit(
            request=request,
            action="review.featured_changed",
            object_type="Review",
            object_reference="bulk",
            summary="Reviews were featured.",
            metadata={"count": updated, "status": "featured"},
        )
        self.message_user(request, f"تم تمييز {updated} مراجعة.", messages.SUCCESS)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: Review | None = None,
    ) -> bool:
        return request.user.is_superuser
