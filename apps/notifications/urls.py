from django.urls import path

from apps.reservations import operations_views

from . import health, views

app_name = "notifications"

urlpatterns = [
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
