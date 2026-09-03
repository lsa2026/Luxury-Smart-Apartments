from django.contrib import admin
from django.http import HttpRequest

from .models import PaymentAttempt


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "booking_intent",
        "modification_request",
        "provider",
        "provider_reference",
        "merchant_transaction_id",
        "amount",
        "currency",
        "status",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "provider", "currency", "created_at")
    search_fields = (
        "booking_intent__public_reference",
        "modification_request__public_reference",
        "provider_reference",
        "provider_checkout_id",
        "provider_payment_id",
        "merchant_transaction_id",
        "provider_result_code",
    )
    date_hierarchy = "created_at"
    readonly_fields = (
        "id",
        "booking_intent",
        "modification_request",
        "provider",
        "provider_reference",
        "provider_checkout_id",
        "provider_payment_id",
        "merchant_transaction_id",
        "widget_integrity",
        "provider_result_code",
        "provider_result_description",
        "amount",
        "currency",
        "status",
        "idempotency_key",
        "failure_code",
        "verified_at",
        "created_at",
        "updated_at",
    )

    def get_search_fields(self, request: HttpRequest) -> tuple[str, ...]:
        fields = self.search_fields
        if request.user.is_superuser or request.user.has_perm(
            "reservations.view_bookingintent_pii"
        ):
            return fields + ("booking_intent__guest_email",)
        return fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: PaymentAttempt | None = None
    ) -> bool:
        return request.user.has_perm("payments.view_paymentattempt")

    def has_delete_permission(
        self, request: HttpRequest, obj: PaymentAttempt | None = None
    ) -> bool:
        return False
