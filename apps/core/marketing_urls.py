from django.urls import path

from .marketing_views import (
    marketing_diagnostics,
    seo_dashboard,
    validate_marketing_component,
)

app_name = "marketing"

urlpatterns = [
    path("admin/marketing/diagnostics/", marketing_diagnostics, name="diagnostics"),
    path("admin/marketing/seo/", seo_dashboard, name="seo_dashboard"),
    path(
        "admin/marketing/diagnostics/validate/<slug:component>/",
        validate_marketing_component,
        name="validate",
    ),
]
