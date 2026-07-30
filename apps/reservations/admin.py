"""Read-mostly administration for local quotes and booking intents."""

import json

from django.contrib import admin, messages
from django.contrib.admin import ModelAdmin
from django.http import HttpRequest
from django.utils import timezone

from .models import (
    BookingIntent,
    BookingModificationRequest,
    BookingQuote,
    HostawayModificationOperation,
    HostawayReservationOperation,
    Reservation,
)


@admin.register(BookingQuote)
class BookingQuoteAdmin(ModelAdmin):
    list_display = (
        "property",
        "check_in",
        "check_out",
        "guests",
        "total_price",
        "currency",
        "status",
        "expires_at",
    )
    list_filter = ("status", "property", "check_in", "expires_at")
    search_fields = ("property__name_ar", "property__name_en")
    date_hierarchy = "created_at"
    fields = (
        "id",
        "property",
        "check_in",
        "check_out",
        "nights",
        "guests",
        "currency",
        "total_price",
        "components_display",
        "price_version",
        "status",
        "expires_at",
        "calculated_at",
        "consumed_at",
        "invalidated_at",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields

    @admin.display(description="مكونات السعر")
    def components_display(self, obj: BookingQuote) -> str:
        return json.dumps(obj.components, ensure_ascii=False, indent=2)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: BookingQuote | None = None,
    ) -> bool:
        return request.user.is_superuser


@admin.register(BookingIntent)
class BookingIntentAdmin(ModelAdmin):
    list_display = (
        "public_reference",
        "property",
        "check_in",
        "check_out",
        "guests",
        "total_price",
        "currency",
        "status",
        "expires_at",
    )
    list_filter = ("status", "property", "check_in", "expires_at")
    search_fields = (
        "public_reference",
        "property__name_ar",
        "property__name_en",
    )
    date_hierarchy = "created_at"
    actions = ("cancel_selected",)
    base_fields = (
        "public_reference",
        "quote",
        "property",
        "check_in",
        "check_out",
        "nights",
        "guests",
        "currency",
        "total_price",
        "status",
        "terms_accepted_at",
        "privacy_accepted_at",
        "marketing_consent",
        "expires_at",
        "created_at",
        "updated_at",
    )
    pii_fields = (
        "guest_first_name",
        "guest_last_name",
        "guest_email",
        "guest_phone",
        "guest_country_code",
        "special_requests",
    )

    def get_fields(
        self,
        request: HttpRequest,
        obj: BookingIntent | None = None,
    ) -> tuple[str, ...]:
        if request.user.is_superuser or request.user.has_perm(
            "reservations.view_bookingintent_pii"
        ):
            return self.base_fields[:10] + self.pii_fields + self.base_fields[10:]
        return self.base_fields

    def get_readonly_fields(
        self,
        request: HttpRequest,
        obj: BookingIntent | None = None,
    ) -> tuple[str, ...]:
        return self.get_fields(request, obj)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: BookingIntent | None = None,
    ) -> bool:
        return request.user.is_superuser

    @admin.action(description="إلغاء الطلبات المبدئية المحددة")
    def cancel_selected(
        self,
        request: HttpRequest,
        queryset: object,
    ) -> None:
        if not (
            request.user.is_superuser or request.user.has_perm("reservations.cancel_bookingintent")
        ):
            self.message_user(request, "لا تملك صلاحية الإلغاء.", messages.ERROR)
            return
        cancellable = queryset.exclude(
            status__in=(
                BookingIntent.Status.COMPLETED,
                BookingIntent.Status.CANCELLED,
                BookingIntent.Status.EXPIRED,
            )
        )
        audited_objects = list(cancellable)
        count = cancellable.update(
            status=BookingIntent.Status.CANCELLED,
            updated_at=timezone.now(),
        )
        for obj in audited_objects:
            self.log_change(request, obj, "Cancelled through the admin action.")
        self.message_user(request, f"تم إلغاء {count} طلبًا.")


@admin.register(Reservation)
class ReservationAdmin(ModelAdmin):
    list_display = (
        "public_reference",
        "property",
        "source_type",
        "normalized_status",
        "hostaway_status",
        "hostaway_reservation_id",
        "check_in",
        "check_out",
        "guests",
        "currency",
        "total_price",
        "last_synced_at",
    )
    list_filter = ("source_type", "normalized_status", "hostaway_status", "check_in")
    search_fields = (
        "public_reference",
        "hostaway_reservation_id",
        "property__name_ar",
        "property__name_en",
    )
    readonly_fields = (
        "id",
        "public_reference",
        "booking_intent",
        "property",
        "hostaway_reservation_id",
        "hostaway_listing_id",
        "hostaway_listing_map_id",
        "channel_id",
        "source_type",
        "normalized_status",
        "hostaway_status",
        "payment_status",
        "check_in",
        "check_out",
        "nights",
        "guests",
        "currency",
        "total_price",
        "source_updated_at",
        "last_synced_at",
        "confirmed_at",
        "cancelled_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: Reservation | None = None,
    ) -> bool:
        return False


@admin.register(HostawayReservationOperation)
class HostawayReservationOperationAdmin(ModelAdmin):
    list_display = (
        "reservation",
        "operation_type",
        "status",
        "attempt_count",
        "hostaway_reservation_id",
        "error_code",
        "created_at",
    )
    list_filter = ("operation_type", "status", "created_at")
    search_fields = ("reservation__public_reference", "error_code")
    actions = ("mark_unknown_for_review",)
    readonly_fields = (
        "id",
        "reservation",
        "operation_type",
        "idempotency_key",
        "fingerprint_preview",
        "status",
        "attempt_count",
        "hostaway_reservation_id",
        "error_code",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    )
    exclude = ("request_fingerprint",)

    @admin.display(description="بصمة الطلب")
    def fingerprint_preview(self, obj: HostawayReservationOperation) -> str:
        return f"{obj.request_fingerprint[:8]}…"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: HostawayReservationOperation | None = None,
    ) -> bool:
        return False

    @admin.action(description="وضع العمليات غير المؤكدة بانتظار مراجعة")
    def mark_unknown_for_review(self, request: HttpRequest, queryset: object) -> None:
        if not request.user.is_superuser:
            self.message_user(request, "يتطلب الإجراء صلاحية عليا.", messages.ERROR)
            return
        count = queryset.filter(status=HostawayReservationOperation.Status.UNKNOWN).update(
            status=HostawayReservationOperation.Status.BLOCKED,
            error_code="admin_review_required",
            updated_at=timezone.now(),
        )
        self.message_user(request, f"تم وضع {count} عملية للمراجعة دون إعادة إرسال.")


@admin.register(BookingModificationRequest)
class BookingModificationRequestAdmin(ModelAdmin):
    list_display = (
        "public_reference",
        "reservation",
        "request_type",
        "status",
        "old_check_in",
        "old_check_out",
        "new_check_in",
        "new_check_out",
        "price_difference",
        "currency",
        "requested_at",
        "completed_at",
    )
    list_filter = ("request_type", "status", "currency", "requested_at")
    search_fields = ("public_reference", "reservation__public_reference")
    actions = ("approve_locally", "reject_locally")
    fields = (
        "id",
        "public_reference",
        "reservation",
        "request_type",
        "status",
        "old_check_in",
        "old_check_out",
        "new_check_in",
        "new_check_out",
        "old_guests",
        "new_guests",
        "old_total",
        "new_total",
        "price_difference",
        "currency",
        "reason",
        "quote_summary",
        "requested_at",
        "expires_at",
        "approved_at",
        "rejected_at",
        "completed_at",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields
    exclude = ("quote_snapshot", "idempotency_key", "session_key_hash")

    @admin.display(description="ملخص عرض السعر")
    def quote_summary(self, obj: BookingModificationRequest) -> str:
        components = obj.quote_snapshot.get("components", [])
        return f"priceDetails v2 — components: {len(components)}"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: BookingModificationRequest | None = None,
    ) -> bool:
        return False

    @admin.action(description="موافقة محلية دون إرسال إلى Hostaway")
    def approve_locally(self, request: HttpRequest, queryset: object) -> None:
        if not request.user.has_perm("reservations.approve_bookingmodificationrequest"):
            self.message_user(request, "لا تملك صلاحية الموافقة.", messages.ERROR)
            return
        now = timezone.now()
        count = queryset.filter(
            status=BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
        ).update(
            status=BookingModificationRequest.Status.READY_FOR_HOSTAWAY,
            approved_at=now,
            updated_at=now,
        )
        self.message_user(request, f"تمت الموافقة المحلية على {count} طلب دون إرسال.")

    @admin.action(description="رفض الطلب محليًا")
    def reject_locally(self, request: HttpRequest, queryset: object) -> None:
        if not request.user.has_perm("reservations.reject_bookingmodificationrequest"):
            self.message_user(request, "لا تملك صلاحية الرفض.", messages.ERROR)
            return
        now = timezone.now()
        count = queryset.exclude(
            status__in=(
                BookingModificationRequest.Status.COMPLETED,
                BookingModificationRequest.Status.REJECTED,
            )
        ).update(
            status=BookingModificationRequest.Status.REJECTED,
            rejected_at=now,
            updated_at=now,
        )
        self.message_user(request, f"تم رفض {count} طلب محليًا.")


@admin.register(HostawayModificationOperation)
class HostawayModificationOperationAdmin(ModelAdmin):
    list_display = (
        "modification_request",
        "operation_type",
        "status",
        "attempt_count",
        "error_code",
        "created_at",
    )
    list_filter = ("operation_type", "status", "created_at")
    search_fields = ("modification_request__public_reference", "error_code")
    actions = ("mark_unknown_for_review",)
    fields = (
        "id",
        "modification_request",
        "operation_type",
        "idempotency_key",
        "fingerprint_preview",
        "status",
        "attempt_count",
        "hostaway_reservation_id",
        "error_code",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields
    exclude = ("request_fingerprint",)

    @admin.display(description="بصمة الطلب")
    def fingerprint_preview(self, obj: HostawayModificationOperation) -> str:
        return f"{obj.request_fingerprint[:8]}…"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: HostawayModificationOperation | None = None,
    ) -> bool:
        return False

    @admin.action(description="وضع العمليات غير المؤكدة بانتظار مراجعة")
    def mark_unknown_for_review(self, request: HttpRequest, queryset: object) -> None:
        if not request.user.is_superuser:
            self.message_user(request, "يتطلب الإجراء صلاحية عليا.", messages.ERROR)
            return
        count = queryset.filter(status=HostawayModificationOperation.Status.UNKNOWN).update(
            status=HostawayModificationOperation.Status.BLOCKED,
            error_code="admin_review_required",
            updated_at=timezone.now(),
        )
        self.message_user(request, f"تم وضع {count} عملية للمراجعة دون إعادة إرسال.")
