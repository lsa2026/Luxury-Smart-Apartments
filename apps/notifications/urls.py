from django.urls import path

from . import health, views

app_name = "notifications"

urlpatterns = [
    path("health/live/", health.live, name="health_live"),
    path("health/ready/", health.ready, name="health_ready"),
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
