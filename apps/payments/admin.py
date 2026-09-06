from django.contrib import admin
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.core.templatetags.presentation import localized_money
from .models import PaymentAttempt


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "booking_reference",
        "payment_context",
        "provider",
        "amount_list",
        "status",
        "created_at",
        "updated_at",
    )
    list_select_related = ("booking_intent", "modification_request")
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
    fieldsets = (
        (
            _("Payment summary"),
            {
                "fields": (
                    "booking_display",
                    "modification_display",
                    "amount_display",
                    "status_display",
                    "provider_display",
                )
            },
        ),
        (
            _("Provider response"),
            {"fields": ("result_display", "failure_display")},
        ),
        (
            _("Technical references"),
            {
                "classes": ("collapse",),
                "fields": ("technical_display",),
                "description": _(
                    "Use these references only when investigating the payment provider."
                ),
            },
        ),
        (
            _("Verification timeline"),
            {"classes": ("collapse",), "fields": ("timeline_display",)},
        ),
    )
    readonly_fields = (
        "booking_display",
        "modification_display",
        "amount_display",
        "status_display",
        "provider_display",
        "result_display",
        "failure_display",
        "technical_display",
        "timeline_display",
    )

    @admin.display(description=_("Booking request"), ordering="booking_intent__public_reference")
    def booking_reference(self, obj: PaymentAttempt) -> str:
        return obj.booking_intent.public_reference

    @admin.display(description=_("Payment for"))
    def payment_context(self, obj: PaymentAttempt) -> object:
        if obj.modification_request_id:
            return _("Booking modification")
        return _("Original booking")

    @admin.display(description=_("Amount"), ordering="amount")
    def amount_list(self, obj: PaymentAttempt) -> str:
        return localized_money(obj.amount, obj.currency)

    @admin.display(description=_("Booking request"))
    def booking_display(self, obj: PaymentAttempt) -> str:
        url = reverse("admin:reservations_bookingintent_change", args=(obj.booking_intent_id,))
        return format_html(
            '<a href="{}" dir="ltr">{}</a>',
            url,
            obj.booking_intent.public_reference,
        )

    @admin.display(description=_("Modification request"))
    def modification_display(self, obj: PaymentAttempt) -> str:
        if not obj.modification_request_id:
            return "—"
        url = reverse(
            "admin:reservations_bookingmodificationrequest_change",
            args=(obj.modification_request_id,),
        )
        return format_html(
            '<a href="{}" dir="ltr">{}</a>',
            url,
            obj.modification_request.public_reference,
        )

    @admin.display(description=_("Amount"))
    def amount_display(self, obj: PaymentAttempt) -> str:
        return self.amount_list(obj)

    @admin.display(description=_("Payment status"))
    def status_display(self, obj: PaymentAttempt) -> object:
        return obj.get_status_display()

    @admin.display(description=_("Payment provider"))
    def provider_display(self, obj: PaymentAttempt) -> str:
        return obj.provider.upper()

    @admin.display(description=_("Provider result"))
    def result_display(self, obj: PaymentAttempt) -> str:
        if not obj.provider_result_code and not obj.provider_result_description:
            return "—"
        return format_html(
            '<span dir="ltr">{} · {}</span>',
            obj.provider_result_code or "—",
            obj.provider_result_description or "—",
        )

    @admin.display(description=_("Failure reason"))
    def failure_display(self, obj: PaymentAttempt) -> str:
        return obj.failure_code or "—"

    @admin.display(description=_("Provider technical references"))
    def technical_display(self, obj: PaymentAttempt) -> str:
        values = (
            ("ID", obj.pk),
            ("Provider reference", obj.provider_reference),
            ("Checkout ID", obj.provider_checkout_id),
            ("Payment ID", obj.provider_payment_id),
            ("Merchant transaction ID", obj.merchant_transaction_id),
        )
        return format_html(
            '<div class="lsa-admin-technical" dir="ltr">{}</div>',
            " · ".join(f"{label}: {value or '—'}" for label, value in values),
        )

    @admin.display(description=_("Verification timeline"))
    def timeline_display(self, obj: PaymentAttempt) -> str:
        return format_html(
            '{}: <span dir="ltr">{}</span> · {}: <span dir="ltr">{}</span> · {}: '
            '<span dir="ltr">{}</span>',
            _("Created"),
            obj.created_at,
            _("Updated"),
            obj.updated_at,
            _("Verified"),
            obj.verified_at or "—",
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
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: PaymentAttempt | None = None
    ) -> bool:
        return False
