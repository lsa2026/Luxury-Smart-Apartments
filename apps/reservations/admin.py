"""Read-mostly administration for local quotes and booking intents."""

import json

from django.contrib import admin, messages
from django.contrib.admin import ModelAdmin
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.notifications.services.audit import record_audit

from .models import (
    BookingIntent,
    BookingModificationRequest,
    BookingQuote,
    CancellationPolicyTier,
    HostawayModificationOperation,
    HostawayReservationOperation,
    RefundObligation,
    Reservation,
)

HOSTAWAY_STATUS_LABELS = {
    "new": _("New in Hostaway"),
    "modified": _("Modified in Hostaway"),
    "cancelled": _("Cancelled in Hostaway"),
    "pending": _("Pending in Hostaway"),
    "confirmed": _("Confirmed in Hostaway"),
    "test_only_not_sent": _("Test booking — not sent"),
}

PAYMENT_STATUS_LABELS = {
    "paid": _("Paid"),
    "sandbox_paid": _("Sandbox payment completed"),
    "pending": _("Payment pending"),
    "unpaid": _("Unpaid"),
    "refunded": _("Refunded"),
    "partially_refunded": _("Partially refunded"),
}


def _hostaway_status_label(value: str) -> object:
    return HOSTAWAY_STATUS_LABELS.get((value or "").casefold(), value or _("Not linked"))


def _payment_status_label(value: str) -> object:
    return PAYMENT_STATUS_LABELS.get((value or "").casefold(), value or _("No payment status"))


@admin.register(BookingQuote)
class BookingQuoteAdmin(ModelAdmin):
    list_display = (
        "property",
        "check_in",
        "check_out",
        "guests",
        "total_price",
        "currency",
        "payment_amount_sar",
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
        "payment_amount_sar",
        "selected_display_currency",
        "exchange_rate_summary",
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

    @admin.display(description=_("Exchange-rate snapshot"))
    def exchange_rate_summary(self, obj: BookingQuote) -> str:
        return json.dumps(obj.exchange_rate_snapshot, ensure_ascii=False, indent=2)

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
        "stay_dates_list",
        "guests",
        "amount_list",
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

    def get_list_display(self, request: HttpRequest) -> tuple[str, ...]:
        display = list(self.list_display)
        if request.user.is_superuser or request.user.has_perm(
            "reservations.view_bookingintent_pii"
        ):
            display.insert(2, "guest_name_list")
        return tuple(display)

    def get_fieldsets(
        self,
        request: HttpRequest,
        obj: BookingIntent | None = None,
    ) -> tuple[tuple[object, dict[str, object]], ...]:
        fieldsets: list[tuple[object, dict[str, object]]] = [
            (
                _("Booking request summary"),
                {
                    "fields": (
                        "reference_display",
                        "property_display",
                        "stay_dates_display",
                        "occupancy_display",
                        "amount_display",
                        "payment_amount_display",
                        "intent_status_display",
                    )
                },
            )
        ]
        if request.user.is_superuser or request.user.has_perm(
            "reservations.view_bookingintent_pii"
        ):
            fieldsets.append(
                (
                    _("Protected guest information"),
                    {
                        "fields": (
                            "guest_name_display",
                            "guest_contact_display",
                            "billing_display",
                            "requests_display",
                        )
                    },
                )
            )
        fieldsets.extend(
            [
                (
                    _("Linked records"),
                    {"fields": ("linkage_display",)},
                ),
                (
                    _("Consent and timeline"),
                    {
                        "classes": ("collapse",),
                        "fields": ("consent_display", "intent_timeline_display"),
                    },
                ),
            ]
        )
        return tuple(fieldsets)

    def get_fields(
        self,
        request: HttpRequest,
        obj: BookingIntent | None = None,
    ) -> tuple[str, ...]:
        return tuple(
            field
            for _name, options in self.get_fieldsets(request, obj)
            for field in options["fields"]
        )

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
        return False

    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict[str, object] | None = None,
    ) -> object:
        context = {
            "title": _("View booking request"),
            "show_save": False,
            "show_save_and_continue": False,
            "show_save_and_add_another": False,
        }
        if extra_context:
            context.update(extra_context)
        return super().changeform_view(request, object_id, form_url, context)

    @admin.display(description=_("Stay period"), ordering="check_in")
    def stay_dates_list(self, obj: BookingIntent) -> str:
        return format_html(
            '<span dir="ltr">{} → {}</span>',
            obj.check_in.strftime("%Y-%m-%d"),
            obj.check_out.strftime("%Y-%m-%d"),
        )

    @admin.display(description=_("Guest"))
    def guest_name_list(self, obj: BookingIntent) -> str:
        return f"{obj.guest_first_name} {obj.guest_last_name}".strip() or "—"

    @admin.display(description=_("Booking value"), ordering="total_price")
    def amount_list(self, obj: BookingIntent) -> str:
        return format_html(
            '<span class="lsa-admin-money" dir="ltr">{} {}</span>',
            f"{obj.total_price:,.2f}",
            obj.currency,
        )

    @admin.display(description=_("Booking reference"))
    def reference_display(self, obj: BookingIntent) -> str:
        return format_html('<strong dir="ltr">{}</strong>', obj.public_reference)

    @admin.display(description=_("Property"))
    def property_display(self, obj: BookingIntent) -> object:
        return obj.property

    @admin.display(description=_("Stay period"))
    def stay_dates_display(self, obj: BookingIntent) -> str:
        return format_html(
            '<span dir="ltr">{} → {}</span> · {}',
            obj.check_in.strftime("%Y-%m-%d"),
            obj.check_out.strftime("%Y-%m-%d"),
            _("%(count)d nights") % {"count": obj.nights},
        )

    @admin.display(description=_("Occupancy"))
    def occupancy_display(self, obj: BookingIntent) -> str:
        return _("%(count)d guests") % {"count": obj.guests}

    @admin.display(description=_("Booking value"))
    def amount_display(self, obj: BookingIntent) -> str:
        return self.amount_list(obj)

    @admin.display(description=_("HyperPay amount"))
    def payment_amount_display(self, obj: BookingIntent) -> str:
        if obj.payment_amount_sar is None:
            return "—"
        snapshot = obj.exchange_rate_snapshot
        return format_html(
            '<span class="lsa-admin-money" dir="ltr">{} SAR</span><br>'
            '<small>{} · {}</small>',
            f"{obj.payment_amount_sar:,.2f}",
            snapshot.get("provider", "—"),
            snapshot.get("rate_timestamp", "—"),
        )

    @admin.display(description=_("Request status"))
    def intent_status_display(self, obj: BookingIntent) -> object:
        return obj.get_status_display()

    @admin.display(description=_("Guest name"))
    def guest_name_display(self, obj: BookingIntent) -> str:
        return self.guest_name_list(obj)

    @admin.display(description=_("Contact details"))
    def guest_contact_display(self, obj: BookingIntent) -> str:
        return format_html(
            '<span dir="ltr">{} · {}</span>',
            obj.guest_email or "—",
            obj.guest_phone or "—",
        )

    @admin.display(description=_("Billing address"))
    def billing_display(self, obj: BookingIntent) -> str:
        parts = filter(
            None,
            (
                obj.billing_street1,
                obj.billing_city,
                obj.billing_state,
                obj.billing_postcode,
                obj.billing_country,
            ),
        )
        return "، ".join(parts) or "—"

    @admin.display(description=_("Special requests"))
    def requests_display(self, obj: BookingIntent) -> str:
        return obj.special_requests or "—"

    @admin.display(description=_("Linked booking records"))
    def linkage_display(self, obj: BookingIntent) -> str:
        quote_url = reverse("admin:reservations_bookingquote_change", args=(obj.quote_id,))
        links = format_html('<a href="{}">{}</a>', quote_url, _("View price quote"))
        try:
            reservation = obj.reservation
        except Reservation.DoesNotExist:
            reservation = None
        if reservation is not None:
            reservation_url = reverse(
                "admin:reservations_reservation_change", args=(reservation.pk,)
            )
            links = format_html(
                '{} · <a href="{}">{}</a>',
                links,
                reservation_url,
                _("View confirmed booking"),
            )
        return links

    @admin.display(description=_("Customer consent"))
    def consent_display(self, obj: BookingIntent) -> str:
        marketing = _("Yes") if obj.marketing_consent else _("No")
        return format_html(
            '{}: <span dir="ltr">{}</span> · {}: <span dir="ltr">{}</span> · {}: {}',
            _("Terms accepted"),
            obj.terms_accepted_at,
            _("Privacy accepted"),
            obj.privacy_accepted_at,
            _("Marketing consent"),
            marketing,
        )

    @admin.display(description=_("Timeline"))
    def intent_timeline_display(self, obj: BookingIntent) -> str:
        return format_html(
            '{}: <span dir="ltr">{}</span> · {}: <span dir="ltr">{}</span> · {}: '
            '<span dir="ltr">{}</span>',
            _("Created"),
            obj.created_at,
            _("Updated"),
            obj.updated_at,
            _("Expires"),
            obj.expires_at,
        )

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
        "stay_window",
        "guests",
        "booking_state",
        "booking_value",
        "hostaway_state",
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
            _("Safe booking management"),
            {
                "fields": ("management_display",),
                "description": _(
                    "Use the approved modification workflow so Hostaway, payment and "
                    "the audit log stay consistent."
                ),
            },
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
        "management_display",
        "hostaway_display",
        "technical_display",
        "sync_display",
        "timeline_display",
    )
    empty_value_display = "—"

    @admin.display(description=_("Stay period"), ordering="check_in")
    def stay_window(self, obj: Reservation) -> str:
        return format_html(
            '<span class="lsa-admin-stay" dir="ltr">{} → {}</span>',
            obj.check_in.strftime("%Y-%m-%d"),
            obj.check_out.strftime("%Y-%m-%d"),
        )

    @admin.display(description=_("Booking status"), ordering="normalized_status")
    def booking_state(self, obj: Reservation) -> object:
        return obj.get_normalized_status_display()

    @admin.display(description=_("Booking value"), ordering="total_price")
    def booking_value(self, obj: Reservation) -> str:
        return format_html(
            '<span class="lsa-admin-money" dir="ltr">{} {}</span>',
            f"{obj.total_price:,.2f}",
            obj.currency,
        )

    @admin.display(description=_("Hostaway status"), ordering="hostaway_status")
    def hostaway_state(self, obj: Reservation) -> object:
        return _hostaway_status_label(obj.hostaway_status)

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
        payment_status = _payment_status_label(obj.payment_status)
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
        hostaway_status = (
            f" · Hostaway: {_hostaway_status_label(obj.hostaway_status)}"
            if obj.hostaway_status
            else ""
        )
        return f"{obj.get_source_type_display()}{test_label}{hostaway_status}"

    @admin.display(description=_("Manage the booking"))
    def management_display(self, obj: Reservation) -> str:
        modifications_url = reverse(
            "admin:reservations_bookingmodificationrequest_changelist"
        )
        modifications_url = f"{modifications_url}?reservation__id__exact={obj.pk}"
        operations_url = reverse(
            "admin:reservations_hostawayreservationoperation_changelist"
        )
        operations_url = f"{operations_url}?reservation__id__exact={obj.pk}"
        return format_html(
            '<div class="lsa-admin-action-hub">'
            '<p>{}</p><a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a></div>',
            _(
                "Changes and cancellations are handled as audited requests; "
                "the confirmed reservation remains read-only."
            ),
            modifications_url,
            _("View modification and cancellation requests"),
            operations_url,
            _("View Hostaway execution log"),
        )

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
        "payment_amount_sar",
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
        fx = obj.quote_snapshot.get("fx", {})
        provider = fx.get("provider", "—") if isinstance(fx, dict) else "—"
        return f"priceDetails v2 — components: {len(components)} — FX: {provider}"

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


@admin.register(CancellationPolicyTier)
class CancellationPolicyTierAdmin(ModelAdmin):
    """Hostaway names the policy; these rows decide what it actually returns."""

    list_display = (
        "policy_code",
        "min_hours_before_check_in",
        "refund_percentage",
        "refunds_cleaning_fee",
        "is_active",
        "note",
    )
    list_filter = ("policy_code", "is_active", "refunds_cleaning_fee")
    list_editable = ("refund_percentage", "refunds_cleaning_fee", "is_active")
    search_fields = ("policy_code", "note")
    ordering = ("policy_code", "-min_hours_before_check_in")

    def get_readonly_fields(
        self,
        request: HttpRequest,
        obj: CancellationPolicyTier | None = None,
    ) -> tuple[str, ...]:
        return ("created_at", "updated_at")


@admin.register(RefundObligation)
class RefundObligationAdmin(ModelAdmin):
    """Money owed to a guest. The transfer happens in the bank, not here."""

    list_display = (
        "public_reference",
        "amount",
        "currency",
        "reason",
        "status",
        "guest_contact",
        "created_at",
    )
    list_filter = ("status", "reason", "currency")
    search_fields = (
        "public_reference",
        "reservation__public_reference",
        "transfer_reference",
    )
    ordering = ("-created_at",)
    actions = ("mark_transferred",)
    readonly_fields = (
        "public_reference",
        "reservation",
        "modification_request",
        "reason",
        "amount",
        "currency",
        "calculation_display",
        "guest_contact",
        "transferred_at",
        "transferred_by",
        "created_at",
        "updated_at",
    )
    fields = readonly_fields + ("status", "transfer_reference", "note")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: RefundObligation | None = None,
    ) -> bool:
        return False

    @admin.display(description=_("Guest contact"))
    def guest_contact(self, obj: RefundObligation) -> str:
        """Shown in full: the administration has to reach this guest to pay them."""
        parts = [obj.guest_name, obj.guest_email, obj.guest_phone]
        return " · ".join(part for part in parts if part) or "—"

    @admin.display(description=_("How the amount was calculated"))
    def calculation_display(self, obj: RefundObligation) -> str:
        if not obj.calculation:
            return "—"
        return format_html(
            "<pre style='white-space:pre-wrap;margin:0'>{}</pre>",
            json.dumps(obj.calculation, ensure_ascii=False, indent=2),
        )

    @admin.action(description=_("Mark as transferred to the guest"))
    def mark_transferred(self, request: HttpRequest, queryset: object) -> None:
        if not request.user.has_perm("reservations.change_refundobligation"):
            self.message_user(
                request,
                _("You do not have permission to settle refunds."),
                messages.ERROR,
            )
            return
        now = timezone.now()
        pending = list(queryset.filter(status=RefundObligation.Status.DUE))
        for obligation in pending:
            obligation.status = RefundObligation.Status.TRANSFERRED
            obligation.transferred_at = now
            obligation.transferred_by = request.user
            obligation.save(
                update_fields=["status", "transferred_at", "transferred_by", "updated_at"]
            )
        record_audit(
            request=request,
            action="refund.marked_transferred",
            object_type="RefundObligation",
            object_reference="bulk",
            summary="Refund obligations were marked as transferred to guests.",
            metadata={"count": len(pending)},
        )
        self.message_user(
            request,
            _("Marked %(count)d refund(s) as transferred.") % {"count": len(pending)},
        )
