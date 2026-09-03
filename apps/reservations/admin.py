"""Read-mostly administration for local quotes and booking intents."""

import json

from django.contrib import admin, messages
from django.contrib.admin import ModelAdmin
from django.http import HttpRequest
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.notifications.services.audit import record_audit

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

    @admin.display(description=_("Price components"))
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
        "customer",
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
        "customer",
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
        "billing_street1",
        "billing_city",
        "billing_state",
        "billing_country",
        "billing_postcode",
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
            return self.base_fields[:11] + self.pii_fields + self.base_fields[11:]
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

    @admin.action(description=_("Cancel the selected booking requests"))
    def cancel_selected(
        self,
        request: HttpRequest,
        queryset: object,
    ) -> None:
        if not (
            request.user.is_superuser or request.user.has_perm("reservations.cancel_bookingintent")
        ):
            self.message_user(
                request,
                _("You do not have permission to cancel."),
                messages.ERROR,
            )
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
        self.message_user(
            request,
            _("Cancelled %(count)d request(s).") % {"count": count},
        )


@admin.register(Reservation)
class ReservationAdmin(ModelAdmin):
    list_display = (
        "public_reference",
        "property",
        "is_test",
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
    fieldsets = (
        (
            _("Stay summary"),
            {
                "fields": (
                    "reference_display",
                    "property_display",
                    "stay_dates_display",
                    "occupancy_display",
                )
            },
        ),
        (
            _("Status and collection"),
            {"fields": ("status_display", "amount_display", "source_display")},
        ),
        (
            _("Hostaway technical integration"),
            {
                "classes": ("collapse",),
                "fields": ("hostaway_display", "technical_display"),
                "description": _(
                    "Reference identifiers for search and diagnostics only; "
                    "do not edit the booking from this screen."
                ),
            },
        ),
        (
            _("Timing and sync log"),
            {
                "classes": ("collapse",),
                "fields": ("sync_display", "timeline_display"),
            },
        ),
    )
    readonly_fields = tuple(field.name for field in Reservation._meta.fields) + (
        "reference_display",
        "property_display",
        "stay_dates_display",
        "occupancy_display",
        "status_display",
        "amount_display",
        "source_display",
        "hostaway_display",
        "technical_display",
        "sync_display",
        "timeline_display",
    )
    empty_value_display = "—"

    @admin.display(description=_("Booking reference"))
    def reference_display(self, obj: Reservation) -> str:
        return format_html('<strong dir="ltr">{}</strong>', obj.public_reference)

    @admin.display(description=_("Property"))
    def property_display(self, obj: Reservation) -> object:
        return obj.property or self.empty_value_display

    @admin.display(description=_("Stay period"))
    def stay_dates_display(self, obj: Reservation) -> str:
        return format_html(
            '<span dir="ltr">{} → {}</span> · {}',
            obj.check_in.strftime("%Y-%m-%d"),
            obj.check_out.strftime("%Y-%m-%d"),
            _("%(count)d nights") % {"count": obj.nights},
        )

    @admin.display(description=_("Occupancy"))
    def occupancy_display(self, obj: Reservation) -> str:
        return _("%(count)d guests") % {"count": obj.guests}

    @admin.display(description=_("Booking and payment status"))
    def status_display(self, obj: Reservation) -> str:
        payment_status = obj.payment_status or _("No payment status")
        return f"{obj.get_normalized_status_display()} · {payment_status}"

    @admin.display(description=_("Booking value"))
    def amount_display(self, obj: Reservation) -> str:
        return format_html(
            '<strong dir="ltr">{} {}</strong>',
            f"{obj.total_price:,.2f}",
            obj.currency,
        )

    @admin.display(description=_("Booking source"))
    def source_display(self, obj: Reservation) -> str:
        test_label = f" · {_('test data')}" if obj.is_test else ""
        hostaway_status = f" · Hostaway: {obj.hostaway_status}" if obj.hostaway_status else ""
        return f"{obj.get_source_type_display()}{test_label}{hostaway_status}"

    @admin.display(description=_("Hostaway identifiers"))
    def hostaway_display(self, obj: Reservation) -> str:
        return format_html(
            '<span dir="ltr">Reservation: {} · Listing: {} · Mapping: {}</span>',
            obj.hostaway_reservation_id or "—",
            obj.hostaway_listing_id or "—",
            obj.hostaway_listing_map_id or "—",
        )

    @admin.display(description=_("Local integration identifiers"))
    def technical_display(self, obj: Reservation) -> str:
        return format_html(
            '<span dir="ltr">ID: {} · Intent: {} · Channel: {}</span>',
            obj.pk,
            obj.booking_intent_id or "—",
            obj.channel_id or "—",
        )

    @admin.display(description=_("Sync"))
    def sync_display(self, obj: Reservation) -> str:
        return format_html(
            '<span dir="ltr">Source: {} · Local sync: {}</span>',
            obj.source_updated_at or "—",
            obj.last_synced_at or "—",
        )

    @admin.display(description=_("Timeline"))
    def timeline_display(self, obj: Reservation) -> str:
        return format_html(
            '<span dir="ltr">Created: {} · Updated: {} · Confirmed: {} · Cancelled: {}</span>',
            obj.created_at or "—",
            obj.updated_at or "—",
            obj.confirmed_at or "—",
            obj.cancelled_at or "—",
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: Reservation | None = None,
    ) -> bool:
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

    @admin.display(description=_("Request fingerprint"))
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

    @admin.action(description=_("Mark unconfirmed operations as awaiting review"))
    def mark_unknown_for_review(self, request: HttpRequest, queryset: object) -> None:
        if not request.user.is_superuser:
            self.message_user(
                request,
                _("This action requires elevated permissions."),
                messages.ERROR,
            )
            return
        count = queryset.filter(status=HostawayReservationOperation.Status.UNKNOWN).update(
            status=HostawayReservationOperation.Status.BLOCKED,
            error_code="admin_review_required",
            updated_at=timezone.now(),
        )
        self.message_user(
            request,
            _("Marked %(count)d operation(s) for review without resending.")
            % {"count": count},
        )


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

    @admin.display(description=_("Quote summary"))
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

    @admin.action(description=_("Approve locally without sending to Hostaway"))
    def approve_locally(self, request: HttpRequest, queryset: object) -> None:
        if not request.user.has_perm("reservations.approve_bookingmodificationrequest"):
            self.message_user(
                request,
                _("You do not have permission to approve."),
                messages.ERROR,
            )
            return
        now = timezone.now()
        count = queryset.filter(
            status=BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
        ).update(
            status=BookingModificationRequest.Status.READY_FOR_HOSTAWAY,
            approved_at=now,
            updated_at=now,
        )
        record_audit(
            request=request,
            action="modification.approved_locally",
            object_type="BookingModificationRequest",
            object_reference="bulk",
            summary="Modification requests were approved locally without Hostaway writes.",
            metadata={"count": count, "status": "ready_for_hostaway"},
        )
        self.message_user(
            request,
            _("Approved %(count)d request(s) locally without sending.")
            % {"count": count},
        )

    @admin.action(description=_("Reject the request locally"))
    def reject_locally(self, request: HttpRequest, queryset: object) -> None:
        if not request.user.has_perm("reservations.reject_bookingmodificationrequest"):
            self.message_user(
                request,
                _("You do not have permission to reject."),
                messages.ERROR,
            )
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
        record_audit(
            request=request,
            action="modification.rejected_locally",
            object_type="BookingModificationRequest",
            object_reference="bulk",
            summary="Modification requests were rejected locally.",
            metadata={"count": count, "status": "rejected"},
        )
        self.message_user(
            request,
            _("Rejected %(count)d request(s) locally.") % {"count": count},
        )


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

    @admin.display(description=_("Request fingerprint"))
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

    @admin.action(description=_("Mark unconfirmed operations as awaiting review"))
    def mark_unknown_for_review(self, request: HttpRequest, queryset: object) -> None:
        if not request.user.is_superuser:
            self.message_user(
                request,
                _("This action requires elevated permissions."),
                messages.ERROR,
            )
            return
        count = queryset.filter(status=HostawayModificationOperation.Status.UNKNOWN).update(
            status=HostawayModificationOperation.Status.BLOCKED,
            error_code="admin_review_required",
            updated_at=timezone.now(),
        )
        self.message_user(
            request,
            _("Marked %(count)d operation(s) for review without resending.")
            % {"count": count},
        )
