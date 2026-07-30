"""Read-mostly administration for local quotes and booking intents."""

import json

from django.contrib import admin, messages
from django.contrib.admin import ModelAdmin
from django.http import HttpRequest
from django.utils import timezone

from .models import BookingIntent, BookingQuote


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
