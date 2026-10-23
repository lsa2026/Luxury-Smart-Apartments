from django.urls import path

from apps.reservations.morning_communications import read_event

from .views import hostaway_unified_webhook

app_name = "integrations"

urlpatterns = [
    path("morning-events/<uuid:event_id>/", read_event, name="morning_event"),
    path(
        "hostaway/webhooks/unified/",
        hostaway_unified_webhook,
        name="hostaway_unified_webhook",
    ),
]
