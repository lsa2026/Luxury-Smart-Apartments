"""Guest-requested invoices through the existing private issuance/delivery services."""

import hashlib
import uuid

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.integrations.hostaway.client import HostawayClient
from apps.notifications.invoice_forms import InvoiceReviewForm, InvoiceSendForm
from apps.notifications.models import GuestInvoice, GuestInvoiceDelivery
from apps.notifications.services.audit import record_audit
from apps.notifications.services.invoice_search import find_invoice_bookings, stored_phone
from apps.notifications.services.invoices import (
    invoice_snapshot,
    issue_invoice,
    queue_invoice_delivery,
    snapshot_token,
)

from .sama_booking_api import actor, authenticated, payload, response
from .services.sama_guest_context import canonical_phone

SALT = "sma-guest-invoice-v1"


def enabled():
    return getattr(settings, "SAMA_INVOICES_ENABLED", False)


def phone_digest(phone):
    return hashlib.sha256(phone.encode()).hexdigest()


def valid_phone(value):
    return isinstance(value, str) and value.startswith("+") and canonical_phone(value) == value


def token(phone, **fields):
    return signing.dumps({"phone_key": phone_digest(phone), **fields}, salt=SALT)


def read_token(value, phone):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValidationError("Invalid invoice selection.")
    data = signing.loads(value, salt=SALT, max_age=900)
    if data.get("phone_key") != phone_digest(phone):
        raise ValidationError("Invalid invoice selection.")
    return data


def option(phone, booking_id, snapshot):
    return {
        "selection": token(phone, reservation_id=booking_id),
        "property_names": snapshot.get("property_names", {}),
        "check_in": snapshot["check_in"],
        "check_out": snapshot["check_out"],
    }


@authenticated("POST")
def options(request):
    if not enabled():
        return response("disabled", 503)
    data = payload(request, ("phone", "cursor"))
    if data is None or not valid_phone(data["phone"]):
        return response("invalid_request", 400)
    phone, cursor = data["phone"], None
    if data["cursor"] is not None:
        try:
            cursor = read_token(data["cursor"], phone)["cursor"]
            if type(cursor) is not int or cursor <= 0:
                raise ValueError
        except (signing.BadSignature, ValidationError, ValueError, KeyError, TypeError):
            return response("selection_expired", 409)
    candidates = {}
    # Previously issued/drafted invoices remain discoverable beyond the latest provider page.
    if cursor is None:
        for invoice in GuestInvoice.objects.filter(snapshot__guest_phone=phone).defer(
            "pdf_content"
        )[:7]:
            candidates[invoice.hostaway_reservation_id] = option(
                phone, invoice.hostaway_reservation_id, invoice.snapshot
            )
    with HostawayClient(max_get_attempts=1, timeout=8) as client:
        found, next_cursor = find_invoice_bookings(client=client, phone=phone, after_id=cursor)
    for row in found:
        candidates[row["id"]] = option(
            phone,
            row["id"],
            {
                "property_names": {
                    "ar": row["property_name"],
                    "en": row["property_name"],
                    "fr": row["property_name"],
                },
                "check_in": row["check_in"],
                "check_out": row["check_out"],
            },
        )
    if len(candidates) > 6:
        return response("review_required")  # Never silently choose among unshown stays.
    return response(
        "invoice_options",
        match_key=phone_digest(phone),
        options=list(candidates.values()),
        cursor=token(phone, cursor=next_cursor) if next_cursor else None,
        coverage="partial" if next_cursor else "complete_page_search",
    )


def projection(delivery):
    return {"id": str(delivery.pk), "channel": delivery.channel, "status": delivery.status}


@authenticated("POST")
def send(request):
    if not enabled():
        return response("disabled", 503)
    data = payload(request, ("phone", "selection", "channels", "email", "language", "confirmed"))
    if (
        data is None
        or not valid_phone(data["phone"])
        or data["confirmed"] is not True
        or data["language"] not in {"ar", "en", "fr"}
        or not isinstance(data["channels"], list)
        or not 1 <= len(data["channels"]) <= 2
        or any(c not in {"email", "whatsapp"} for c in data["channels"])
        or len(set(data["channels"])) != len(data["channels"])
        or not isinstance(data["email"], str)
        or ("email" not in data["channels"] and data["email"] != "")
    ):
        return response("invalid_request", 400)
    forms = []
    for channel in data["channels"]:
        form = InvoiceSendForm(
            {
                "channel": channel,
                "recipient": data["phone"] if channel == "whatsapp" else data["email"],
                "confirm_recipient": True,
            }
        )
        if not form.is_valid():
            return response("invalid_recipient", 400)
        forms.append(form.cleaned_data)
    # Validate both requested channels before issuing anything or queueing either one.
    if any(
        (f["channel"] == "email" and not settings.EMAIL_DELIVERY_ENABLED)
        or (f["channel"] == "whatsapp" and not settings.ULTRAMSG_ENABLED)
        for f in forms
    ):
        return response("delivery_disabled", 503)
    try:
        selected = read_token(data["selection"], data["phone"])
        booking_id = selected["reservation_id"]
        if type(booking_id) is not int or booking_id <= 0:
            raise ValueError
    except (signing.BadSignature, ValidationError, ValueError, KeyError, TypeError):
        return response("selection_expired", 409)
    service_actor = actor()
    with HostawayClient(max_get_attempts=1, timeout=8) as client:
        fresh = client.get_reservation_invoice_data(booking_id)
    if stored_phone(fresh.get("result", {})) != data["phone"]:
        return response("booking_mismatch", 403)
    try:
        checked = invoice_snapshot(fresh, booking_id)
        checked["guest_phone"] = data["phone"]
        # Recipient choice does not rewrite the guest contact frozen into an issued invoice.
        review = InvoiceReviewForm(
            {
                **checked,
                "language": data["language"],
                "confirm_details": True,
                "snapshot_token": "0" * 64,
            }
        )
        if not review.is_valid():
            return response("invoice_details_incomplete")
        with transaction.atomic():
            # Lock the independent invoice, matching the existing owner issuance/send order.
            invoice, _ = GuestInvoice.objects.get_or_create(
                hostaway_reservation_id=booking_id,
                defaults={
                    "created_by": service_actor,
                    "snapshot": checked,
                    "language": data["language"],
                },
            )
            invoice = GuestInvoice.objects.select_for_update().get(pk=invoice.pk)
            if invoice.issued_at:
                stable_fields = (
                    "total",
                    "currency",
                    "check_in",
                    "check_out",
                    "platform_reference",
                    "guest_phone",
                )
                if any(invoice.snapshot.get(k) != checked.get(k) for k in stable_fields):
                    return response("invoice_changed_review_required")
            else:
                invoice.snapshot = checked
                invoice.save(update_fields=["snapshot"])
                reviewed = {**review.cleaned_data, "snapshot_token": snapshot_token(invoice)}
                invoice = issue_invoice(invoice.pk, reviewed)
            deliveries = [
                queue_invoice_delivery(
                    invoice=invoice,
                    channel=f["channel"],
                    recipient=f["recipient"],
                    actor=service_actor,
                    retry_failed=False,
                    delay_seconds=15,  # Let SMA record the request acknowledgement before the PDF.
                )
                for f in forms
            ]
            request.user = service_actor
            record_audit(
                request=request,
                action="sama.invoice_requested",
                object_type="GuestInvoice",
                object_reference=str(invoice.pk),
                summary="Guest-requested invoice; provider outcome tracked separately.",
                metadata={"fields": data["channels"], "source": "sama"},
            )
    except ValidationError:
        return response("invoice_details_incomplete")
    return response("invoice_requested", deliveries=[projection(d) for d in deliveries])


@authenticated("POST")
def status(request):
    if not enabled():
        return response("disabled", 503)
    data = payload(request, ("phone", "deliveries"))
    if (
        data is None
        or not valid_phone(data["phone"])
        or not isinstance(data["deliveries"], list)
        or not 1 <= len(data["deliveries"]) <= 2
    ):
        return response("invalid_request", 400)
    try:
        identifiers = [uuid.UUID(v) for v in data["deliveries"]]
    except (ValueError, TypeError, AttributeError):
        return response("invalid_request", 400)
    deliveries = list(
        GuestInvoiceDelivery.objects.filter(
            pk__in=identifiers,
            invoice__snapshot__guest_phone=data["phone"],
        )
    )
    if len(deliveries) != len(set(identifiers)):
        return response("booking_mismatch", 403)
    return response("invoice_status", deliveries=[projection(d) for d in deliveries])
