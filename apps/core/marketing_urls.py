from django.urls import path

from .campaign_links import missed_call_welcome_redirect
from .marketing_views import (
    acknowledge_purchase_event,
    marketing_diagnostics,
    seo_dashboard,
    validate_marketing_component,
)

app_name = "marketing"

urlpatterns = [
    path("hello", missed_call_welcome_redirect, name="missed_call_welcome"),
    path("hello/", missed_call_welcome_redirect, name="missed_call_welcome_slash"),
    path(
        "analytics/purchase/acknowledge/",
        acknowledge_purchase_event,
        name="purchase_acknowledge",
    ),
    path("admin/marketing/diagnostics/", marketing_diagnostics, name="diagnostics"),
    path("admin/marketing/seo/", seo_dashboard, name="seo_dashboard"),
    path(
        "admin/marketing/diagnostics/validate/<slug:component>/",
        validate_marketing_component,
        name="validate",
    ),
]
