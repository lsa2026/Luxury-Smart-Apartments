from django.urls import path

from .views import hostaway_unified_webhook

app_name = "integrations"

urlpatterns = [
    path(
        "hostaway/webhooks/unified/",
        hostaway_unified_webhook,
        name="hostaway_unified_webhook",
    ),
]
