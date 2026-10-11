"""One explicit, auditable send; ambiguous outcomes are never blindly retried."""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import GuestInvoiceDelivery
from apps.notifications.services.email import EmailMessageRequest, build_guest_email
from apps.notifications.services.invoice_email import invoice_email_context, invoice_filename
from apps.notifications.services.ultramsg import (
    UltraMsgClient,
    UltraMsgConfigurationError,
    UltraMsgConnectionError,
    UltraMsgResponseError,
)

MESSAGES = {
    "ar": (
        "فاتورة إقامتك — Luxury Living",
        "تجد فاتورة إقامتك مرفقة. شكرًا لاختيارك Luxury Living.",
    ),
    "en": (
        "Your accommodation invoice — Luxury Living",
        "Your accommodation invoice is attached. Thank you for choosing Luxury Living.",
    ),
    "fr": (
        "Votre facture de séjour — Luxury Living",
        "Votre facture de séjour est jointe. Merci d’avoir choisi Luxury Living.",
    ),
}


def deliver_invoice(delivery_id):
    with transaction.atomic():
        delivery = GuestInvoiceDelivery.objects.select_for_update().get(pk=delivery_id)
        if delivery.status != "queued":
            return delivery.status
        delivery.status = "sending"
        delivery.attempt_count += 1
        delivery.save(update_fields=["status", "attempt_count"])
    invoice = delivery.invoice
    status, code = "review", "delivery_outcome_unknown"
    try:
        if not invoice.issued_at or not invoice.pdf_content:
            status, code = "failed", "invoice_not_issued"
        else:
            subject, body = MESSAGES[invoice.language]
            pdf_content = bytes(
                delivery.presentation.pdf_content
                if delivery.presentation_id
                else invoice.pdf_content
            )
            filename = invoice_filename(invoice)
            if delivery.channel == "email":
                if not settings.EMAIL_DELIVERY_ENABLED:
                    status, code = "failed", "email_disabled"
                else:
                    message = build_guest_email(
                        EmailMessageRequest(
                            recipient=delivery.recipient,
                            subject=subject,
                            template_name="invoice",
                            language=invoice.language,
                            context=invoice_email_context(invoice),
                        )
                    )
                    message.attach(filename, pdf_content, "application/pdf")
                    if message.send(fail_silently=False) == 1:
                        status, code = "accepted", ""
            elif delivery.channel == "whatsapp":
                with UltraMsgClient() as client:
                    client.send_document(
                        recipient=delivery.recipient,
                        content=pdf_content,
                        filename=filename,
                        caption=body,
                    )
                status, code = "accepted", ""
            else:
                status, code = "failed", "unsupported_channel"
    except UltraMsgConfigurationError:
        status, code = "failed", "whatsapp_disabled"
    except UltraMsgResponseError:
        status, code = "failed", "whatsapp_rejected"
    except UltraMsgConnectionError:
        status, code = "review", "whatsapp_outcome_unknown"
    except Exception:
        # SMTP/network exceptions may follow acceptance. No retries or raw exception logs.
        status, code = "review", "delivery_outcome_unknown"
    GuestInvoiceDelivery.objects.filter(pk=delivery_id, status="sending").update(
        status=status,
        error_code=code,
        accepted_at=timezone.now() if status == "accepted" else None,
    )
    return status
