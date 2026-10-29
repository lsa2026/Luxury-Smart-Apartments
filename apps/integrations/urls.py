from django.urls import path

from apps.loyalty import invitation_api
from apps.reservations import morning07_communications, morning07_gateway

from .views import hostaway_unified_webhook

app_name = "integrations"

urlpatterns = [
    path("loyalty/invitations/health/", invitation_api.health, name="loyalty_invitation_health"),
    path("loyalty/invitations/claim/", invitation_api.claim, name="loyalty_invitation_claim"),
    path(
        "loyalty/invitations/preflight/",
        invitation_api.preflight,
        name="loyalty_invitation_preflight",
    ),
    path("loyalty/invitations/record/", invitation_api.record, name="loyalty_invitation_record"),
    path("loyalty/invitations/status/", invitation_api.status, name="loyalty_invitation_status"),
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
