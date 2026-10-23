from django.urls import path

from apps.loyalty.booking_invitation_previews import preview as loyalty_invitation_preview
from apps.loyalty.welcome_previews import preview as loyalty_welcome_preview
from apps.reservations import morning_views, operations_views

from . import health, invoice_views, views

app_name = "notifications"

urlpatterns = [
    path(
        "admin/operations/loyalty-welcome-preview/",
        loyalty_welcome_preview,
        name="loyalty_welcome_preview",
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
