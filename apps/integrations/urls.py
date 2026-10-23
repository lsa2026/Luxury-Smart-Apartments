from django.urls import path

from apps.reservations import morning07_communications, morning07_gateway

from .views import hostaway_unified_webhook

app_name = "integrations"

urlpatterns = [
    path(
        "morning07/events/<uuid:event_id>/",
        morning07_communications.read_event,
        name="morning07_event",
    ),
    path("morning07/send/", morning07_gateway.send_message, name="morning07_send"),
    path("morning07/receipt/", morning07_gateway.receipt, name="morning07_receipt"),
    path(
        "hostaway/webhooks/unified/",
        hostaway_unified_webhook,
        name="hostaway_unified_webhook",
    ),
]
