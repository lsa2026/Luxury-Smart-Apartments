"""Invoice presentation from the frozen snapshot, without changing its records."""

import re
import unicodedata
from datetime import date
from decimal import Decimal

from django.utils.formats import date_format
from django.utils.translation import override

from apps.notifications.email_design import COPY as DESIGN_COPY
from apps.notifications.invoice_pdf import COPY, platform_name
from apps.notifications.services.invoices import re_internal_reference


def invoice_filename(invoice):
    """ASCII header/document-safe name using only the guest's platform reference."""
    reference = str(invoice.snapshot.get("platform_reference") or "")
    if (
        reference == str(invoice.hostaway_reservation_id)
        or re_internal_reference(reference)
        or any(unicodedata.category(char).startswith("C") for char in reference)
    ):
        return "Invoice.pdf"
    reference = unicodedata.normalize("NFKC", reference)
    reference = re.sub(r"[^A-Za-z0-9_-]+", "-", reference).strip("-_")[:64].rstrip("-_")
    return f"Invoice-{reference}.pdf" if reference else "Invoice.pdf"


def invoice_email_context(invoice):
    language = invoice.language if invoice.language in DESIGN_COPY else "ar"
    data = invoice.snapshot
    with override(language):
        check_in = date_format(date.fromisoformat(data["check_in"]), "j F Y")
        check_out = date_format(date.fromisoformat(data["check_out"]), "j F Y")
    return {
        "heading": DESIGN_COPY[language]["invoice_heading"],
        "message": DESIGN_COPY[language]["invoice_message"],
        "guest_name": data.get("guest_name", ""),
        "property_name": data.get("property_names", {}).get(language)
        or data.get("property_names", {}).get("en", ""),
        "platform_name": platform_name(data.get("platform", "")),
        "platform_reference": data.get("platform_reference", ""),
        "check_in": check_in,
        "check_out": check_out,
        "nights": data["nights"],
        "invoice_total": f"{Decimal(data['total']):,.2f} {data['currency']}",
        "attachment_filename": invoice_filename(invoice),
        "invoice_labels": COPY[language],
    }
