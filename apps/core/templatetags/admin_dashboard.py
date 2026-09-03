from django import template
from django.utils.translation import pgettext_lazy

from apps.core.admin_dashboard import dashboard_payload

register = template.Library()

# These two maps carry their own gettext context so their wording stays independent
# of the model choice labels, which use the same English words with different
# Arabic grammatical forms.
STATUS_LABELS = {
    "active": pgettext_lazy("dashboard status", "Active"),
    "available": pgettext_lazy("dashboard status", "Available"),
    "awaiting_customer_approval": pgettext_lazy("dashboard status", "Awaiting customer approval"),
    "awaiting_payment": pgettext_lazy("dashboard status", "Awaiting payment"),
    "blocked": pgettext_lazy("dashboard status", "Blocked"),
    "cancelled": pgettext_lazy("dashboard status", "Cancelled"),
    "completed": pgettext_lazy("dashboard status", "Completed"),
    "confirmed": pgettext_lazy("dashboard status", "Confirmed"),
    "configured": pgettext_lazy("dashboard status", "Configured"),
    "consumed": pgettext_lazy("dashboard status", "Consumed"),
    "created": pgettext_lazy("dashboard status", "Created"),
    "disabled": pgettext_lazy("dashboard status", "Disabled"),
    "dispatch_disabled": pgettext_lazy("dashboard status", "Task dispatch disabled"),
    "draft": pgettext_lazy("dashboard status", "Draft"),
    "enabled": pgettext_lazy("dashboard status", "Enabled"),
    "expired": pgettext_lazy("dashboard status", "Expired"),
    "failed": pgettext_lazy("dashboard status", "Failed"),
    "invalidated": pgettext_lazy("dashboard status", "Invalidated"),
    "modified": pgettext_lazy("dashboard status", "Modified"),
    "not_configured": pgettext_lazy("dashboard status", "Not configured"),
    "pending": pgettext_lazy("dashboard status", "Pending"),
    "pending_admin_approval": pgettext_lazy("dashboard status", "Awaiting administrator approval"),
    "pending_revalidation": pgettext_lazy("dashboard status", "Awaiting re-verification"),
    "price_changed": pgettext_lazy("dashboard status", "Price changed"),
    "processing": pgettext_lazy("dashboard status", "Processing"),
    "ready_for_hostaway": pgettext_lazy("dashboard status", "Ready for Hostaway"),
    "rejected": pgettext_lazy("dashboard status", "Rejected"),
    "sent": pgettext_lazy("dashboard status", "Sent"),
    "succeeded": pgettext_lazy("dashboard status", "Succeeded"),
    "unavailable": pgettext_lazy("dashboard status", "Unavailable"),
    "unknown": pgettext_lazy("dashboard status", "Unconfirmed"),
}

ADMIN_LABELS = {
    "action checkbox": pgettext_lazy("admin field label", "Select"),
    "amount": pgettext_lazy("admin field label", "Amount"),
    "attempt count": pgettext_lazy("admin field label", "Attempt count"),
    "booking intent": pgettext_lazy("admin field label", "Booking request"),
    "check in": pgettext_lazy("admin field label", "Check-in"),
    "check out": pgettext_lazy("admin field label", "Check-out"),
    "completed at": pgettext_lazy("admin field label", "Completed at"),
    "created at": pgettext_lazy("admin field label", "Created at"),
    "currency": pgettext_lazy("admin field label", "Currency"),
    "dry run": pgettext_lazy("admin field label", "Dry run"),
    "error code": pgettext_lazy("admin field label", "Error code"),
    "event type": pgettext_lazy("admin field label", "Event type"),
    "expires at": pgettext_lazy("admin field label", "Expires at"),
    "failed count": pgettext_lazy("admin field label", "Failure count"),
    "fetched count": pgettext_lazy("admin field label", "Fetched"),
    "guest name": pgettext_lazy("admin field label", "Guest name"),
    "guests": pgettext_lazy("admin field label", "Guests"),
    "hostaway reservation id": pgettext_lazy("admin field label", "Hostaway booking number"),
    "hostaway status": pgettext_lazy("admin field label", "Hostaway status"),
    "is test": pgettext_lazy("admin field label", "Test booking"),
    "language": pgettext_lazy("admin field label", "Language"),
    "last synced at": pgettext_lazy("admin field label", "Last sync"),
    "modification request": pgettext_lazy("admin field label", "Modification request"),
    "new check in": pgettext_lazy("admin field label", "New check-in"),
    "new check out": pgettext_lazy("admin field label", "New check-out"),
    "normalized status": pgettext_lazy("admin field label", "Booking status"),
    "old check in": pgettext_lazy("admin field label", "Previous check-in"),
    "old check out": pgettext_lazy("admin field label", "Previous check-out"),
    "operation type": pgettext_lazy("admin field label", "Operation type"),
    "payment status": pgettext_lazy("admin field label", "Payment status"),
    "price difference": pgettext_lazy("admin field label", "Price difference"),
    "processed at": pgettext_lazy("admin field label", "Processed at"),
    "provider": pgettext_lazy("admin field label", "Payment gateway"),
    "provider reference": pgettext_lazy("admin field label", "Gateway reference"),
    "public reference": pgettext_lazy("admin field label", "Reference"),
    "rating": pgettext_lazy("admin field label", "Rating"),
    "received at": pgettext_lazy("admin field label", "Received at"),
    "recipient masked": pgettext_lazy("admin field label", "Masked recipient"),
    "request type": pgettext_lazy("admin field label", "Request type"),
    "requested at": pgettext_lazy("admin field label", "Requested at"),
    "reservation": pgettext_lazy("admin field label", "Reservation"),
    "source type": pgettext_lazy("admin field label", "Booking source"),
    "started at": pgettext_lazy("admin field label", "Started at"),
    "status": pgettext_lazy("admin field label", "Status"),
    "sync type": pgettext_lazy("admin field label", "Sync type"),
    "total price": pgettext_lazy("admin field label", "Total"),
    "updated at": pgettext_lazy("admin field label", "Last updated"),
    "live_booking": pgettext_lazy("admin field label", "Live booking creation"),
    "live_modification": pgettext_lazy("admin field label", "Live booking modification"),
    "live_extension": pgettext_lazy("admin field label", "Live booking extension"),
    "live_cancellation": pgettext_lazy("admin field label", "Live booking cancellation"),
    "webhook_receiver": pgettext_lazy("admin field label", "Webhook reception"),
    "webhook_processing": pgettext_lazy("admin field label", "Webhook processing"),
}


@register.simple_tag(takes_context=True)
def luxury_dashboard(context: template.Context) -> dict[str, object]:
    return dashboard_payload(context["request"])


@register.filter
def admin_status_label(value: object) -> str:
    """Render internal workflow codes as concise, translated labels."""
    normalized = str(value or "").strip()
    return STATUS_LABELS.get(normalized, normalized.replace("_", " "))


@register.filter
def admin_label(value: object) -> str:
    """Translate common technical Django Admin labels without mutating model state."""
    normalized = str(value or "").strip()
    return ADMIN_LABELS.get(normalized.casefold(), normalized)
