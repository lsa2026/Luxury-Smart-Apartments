from django.urls import path

from apps.accounts.access import operations_access_required
from apps.core import contact_draft_api
from apps.loyalty.booking_invitation_previews import preview as loyalty_invitation_preview
from apps.loyalty.copy_review_previews import preview as loyalty_copy_review_preview
from apps.loyalty.invitation_api import readiness as invitation_integration_readiness
from apps.loyalty.invitation_auth import setup as invitation_credential_setup
from apps.loyalty.welcome_previews import preview as loyalty_welcome_preview
from apps.payments import owner_payment_links
from apps.reservations import day_use_views, morning_views, operations_views, stay_extension_views
from apps.reservations.guest_check_views import guest_classification_check
from apps.reservations.guest_file_views import guest_file, guest_file_for_booking

from . import health, invoice_views, views

app_name = "notifications"

urlpatterns = [
    path(
        "admin/operations/stay-extensions/",
        stay_extension_views.source_list,
        name="stay_extension_sources",
    ),
    path(
        "admin/operations/stay-extensions/<uuid:extension_id>/",
        stay_extension_views.detail,
        name="stay_extension_detail",
    ),
    path("admin/operations/guests/<uuid:file_id>/", guest_file, name="guest_file"),
    path(
        "admin/operations/bookings/<uuid:reservation_id>/guest/",
        guest_file_for_booking,
        name="guest_file_for_booking",
    ),
    path(
        "admin/operations/guest-classification/check/",
        guest_classification_check,
        name="guest_classification_check",
    ),
    path("admin/operations/day-use-stays/", day_use_views.day_use_list, name="day_use_list"),
    path(
        "admin/operations/day-use-stays/new/", day_use_views.day_use_create, name="day_use_create"
    ),
    path(
        "admin/operations/day-use-stays/available-properties/",
        day_use_views.day_use_available_properties,
        name="day_use_available_properties",
    ),
    path(
        "admin/operations/day-use-stays/<uuid:stay_id>/",
        day_use_views.day_use_detail,
        name="day_use_detail",
    ),
    path(
        "admin/operations/morning-address/<str:kind>/",
        morning_views.morning_address,
        name="morning_address",
    ),
    path("reservations/sama/contact-drafts/claim/", contact_draft_api.claim),
    path("reservations/sama/contact-drafts/complete/", contact_draft_api.complete),
    path(
        "admin/operations/loyalty-invitation-credential/",
        invitation_credential_setup,
        name="loyalty_invitation_credential",
    ),
    path(
        "admin/operations/loyalty-invitation-integration/",
        invitation_integration_readiness,
        name="loyalty_invitation_integration",
    ),
    path(
        "admin/operations/loyalty-copy-review/",
        loyalty_copy_review_preview,
        name="loyalty_copy_review_preview",
    ),
    path(
        "admin/operations/loyalty-welcome-preview/",
        loyalty_welcome_preview,
        name="loyalty_welcome_preview",
    ),
    path(
        "admin/operations/morning-stays/<uuid:stay_id>/edit/",
        morning_views.morning_edit,
        name="morning_edit",
    ),
    path(
        "admin/operations/loyalty-invitation-preview/",
        loyalty_invitation_preview,
        name="loyalty_invitation_preview",
    ),
    path("admin/operations/morning-stays/", morning_views.morning_list, name="morning_list"),
    path(
        "admin/operations/morning-stays/available-properties/",
        morning_views.morning_available_properties,
        name="morning_available_properties",
    ),
    path(
        "admin/operations/morning-stays/new/", morning_views.morning_create, name="morning_create"
    ),
    path(
        "admin/operations/morning-stays/<uuid:stay_id>/",
        morning_views.morning_detail,
        name="morning_detail",
    ),
    path(
        "admin/operations/bookings/<uuid:reservation_id>/invoice/",
        invoice_views.booking_invoice,
        name="booking_invoice",
    ),
    path("admin/operations/invoices/", invoice_views.invoice_list, name="invoice_list"),
    path(
        "admin/operations/invoices/<uuid:invoice_id>/",
        invoice_views.invoice_detail,
        name="invoice_detail",
    ),
    path(
        "admin/operations/invoices/<uuid:invoice_id>/issue/",
        invoice_views.invoice_issue,
        name="invoice_issue",
    ),
    path(
        "admin/operations/invoices/<uuid:invoice_id>/pdf/",
        invoice_views.invoice_pdf,
        name="invoice_pdf",
    ),
    path(
        "admin/operations/invoices/<uuid:invoice_id>/refresh-design/",
        invoice_views.invoice_refresh_design,
        name="invoice_refresh_design",
    ),
    path(
        "admin/operations/invoices/<uuid:invoice_id>/send/",
        invoice_views.invoice_send,
        name="invoice_send",
    ),
    path("health/live/", health.live, name="health_live"),
    path("health/ready/", health.ready, name="health_ready"),
    path("admin/operations/hub/", views.operations_hub, name="hub"),
    path(
        "admin/operations/manual-bookings/",
        operations_views.manual_booking_list,
        name="manual_booking_list",
    ),
    path(
        "admin/operations/manual-bookings/new/",
        operations_views.manual_booking_create,
        name="manual_booking_create",
    ),
    path(
        "admin/operations/manual-bookings/available-properties/",
        operations_views.manual_booking_available_properties,
        name="manual_booking_available_properties",
    ),
    path(
        "admin/operations/bookings/",
        operations_views.booking_list,
        name="booking_list",
    ),
    path(
        "admin/operations/bookings/<uuid:reservation_id>/",
        operations_views.booking_detail,
        name="booking_detail",
    ),
    path(
        "admin/operations/bookings/<uuid:reservation_id>/payment-link-review/",
        owner_payment_links.preview_updated_link,
        name="updated_payment_link_preview",
    ),
    path(
        "admin/operations/bookings/<uuid:reservation_id>/payment-link-review/<uuid:preview_id>/",
        owner_payment_links.updated_link_review,
        name="updated_payment_link_review",
    ),
    path(
        "admin/operations/payment-link-reviews/<uuid:reference>/address-suggestions/",
        operations_access_required(owner_payment_links.UpdatedPaymentAddressView.as_view()),
        name="updated_payment_address_suggestions",
    ),
    path(
        "admin/operations/payment-link-reviews/<uuid:reference>/address-details/",
        operations_access_required(
            owner_payment_links.UpdatedPaymentAddressView.as_view(details=True)
        ),
        name="updated_payment_address_details",
    ),
    path(
        "admin/operations/manual-bookings/<uuid:draft_id>/",
        operations_views.manual_booking_detail,
        name="manual_booking_detail",
    ),
    path(
        "admin/operations/cancellations/",
        operations_views.cancellation_list,
        name="cancellation_list",
    ),
    path(
        "admin/operations/cancellations/manual-drafts/<uuid:draft_id>/",
        operations_views.manual_booking_disposal,
        name="manual_booking_disposal",
    ),
    path(
        "admin/operations/cancellations/<uuid:request_id>/",
        operations_views.cancellation_detail,
        name="cancellation_detail",
    ),
    path(
        "admin/operations/refunds/",
        operations_views.refund_list,
        name="refund_list",
    ),
    path(
        "admin/operations/refunds/<uuid:refund_id>/",
        operations_views.refund_detail,
        name="refund_detail",
    ),
    path("admin/operations/", views.operations_dashboard, name="dashboard"),
    path("admin/operations/status/", views.system_status, name="system_status"),
    path("admin/notifications/", views.notification_center, name="center"),
    path(
        "admin/notifications/<uuid:notification_id>/read/",
        views.mark_notification_read,
        name="mark_read",
    ),
    path(
        "admin/notifications/read-all/",
        views.mark_all_notifications_read,
        name="mark_all_read",
    ),
    path(
        "admin/operations/export/<slug:report_name>/",
        views.export_report,
        name="export",
    ),
]
