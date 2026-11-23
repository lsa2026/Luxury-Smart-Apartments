from django.urls import path

from apps.loyalty import invitation_api
from apps.reservations import (
    day_use08_communications,
    day_use08_gateway,
    day_use08_payments,
    day_use_auto_payment,
    day_use_ownership,
    message_receipts,
    message_source_authorization,
    morning07_communications,
    morning07_gateway,
    morning07_payments,
    morning_auto_payment,
    morning_loyalty,
    telegram_authority,
    telegram_stay_journey,
    telegram_stay_source,
    website_financial_source_v2,
)

from .views import hostaway_unified_webhook

app_name = "integrations"

urlpatterns = [
    path(
        "morning07/payment-links/source-authorization/",
        website_financial_source_v2.payment_link_authorization,
        {"scenario": "07"},
        name="morning07_payment_link_source_authorization",
    ),
    path(
        "day-use08/payment-links/source-authorization/",
        website_financial_source_v2.payment_link_authorization,
        {"scenario": "08"},
        name="day_use08_payment_link_source_authorization",
    ),
    path("telegram39/stays/record/", telegram_stay_journey.record, name="telegram39_stay_record"),
    path(
        "telegram39/stays/source/", telegram_stay_source.read_source, name="telegram39_stay_source"
    ),
    path(
        "telegram39/activation-authority/",
        telegram_authority.authority,
        name="telegram39_authority",
    ),
    path("website-messages/receipt/", message_receipts.receipt, name="website_message_receipt"),
    path(
        "morning07/source-authorization/",
        message_source_authorization.source_authorization,
        {"scenario": "07"},
        name="morning07_source_authorization",
    ),
    path(
        "day-use08/source-authorization/",
        message_source_authorization.source_authorization,
        {"scenario": "08"},
        name="day_use08_source_authorization",
    ),
    path(
        "morning07/payments/source-authorization/",
        message_source_authorization.source_authorization,
        {"scenario": "07", "financial": True},
        name="morning07_payment_source_authorization",
    ),
    path(
        "day-use08/payments/source-authorization/",
        message_source_authorization.source_authorization,
        {"scenario": "08", "financial": True},
        name="day_use08_payment_source_authorization",
    ),
    path(
        "day-use08/ownership/<uuid:stay_id>/",
        day_use_ownership.read_ownership,
        name="day_use08_ownership",
    ),
    path(
        "day-use08/events/<uuid:event_id>/",
        day_use08_communications.read_event,
        name="day_use08_event",
    ),
    path("day-use08/send/", day_use08_gateway.send_message, name="day_use08_send"),
    path("day-use08/receipt/", day_use08_gateway.receipt, name="day_use08_receipt"),
    path("day-use08/payment-link/", day_use_auto_payment.after_core, name="day_use08_payment_link"),
    path(
        "day-use08/payments/events/<uuid:event_id>/",
        day_use08_payments.read_event,
        name="day_use08_payment_event",
    ),
    path("day-use08/payments/send/", day_use08_payments.send, name="day_use08_payment_send"),
    path(
        "day-use08/payments/receipt/", day_use08_payments.receipt, name="day_use08_payment_receipt"
    ),
    path(
        "morning07/payments/events/<uuid:event_id>/",
        morning07_payments.read_event,
        name="morning07_payment_event",
    ),
    path("morning07/payments/send/", morning07_payments.send, name="morning07_payment_send"),
    path(
        "morning07/payments/receipt/", morning07_payments.receipt, name="morning07_payment_receipt"
    ),
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
    path("morning07/loyalty/", morning_loyalty.invite, name="morning07_loyalty"),
    path("morning07/payment-link/", morning_auto_payment.after_core, name="morning07_payment_link"),
    path("morning07/send/", morning07_gateway.send_message, name="morning07_send"),
    path("morning07/receipt/", morning07_gateway.receipt, name="morning07_receipt"),
    path(
        "hostaway/webhooks/unified/",
        hostaway_unified_webhook,
        name="hostaway_unified_webhook",
    ),
]
