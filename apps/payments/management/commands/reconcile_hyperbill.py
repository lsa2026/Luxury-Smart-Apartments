"""Sandbox-only read reconciliation, usable without a Celery worker."""

from django.core.management.base import BaseCommand, CommandError

from apps.payments.hyperbill import HyperBillError, reconcile_invoice, require_sandbox
from apps.payments.models import HyperBillInvoice


class Command(BaseCommand):
    help = "Retrieve authoritative HyperBill sandbox invoice status (no collection/refund)."

    def add_arguments(self, parser):
        parser.add_argument("--reference", required=True)

    def handle(self, *args, **options):
        try:
            require_sandbox()
        except HyperBillError as exc:
            raise CommandError(str(exc)) from None
        invoice = HyperBillInvoice.objects.filter(merchant_reference=options["reference"]).first()
        if invoice is None:
            raise CommandError("invoice_not_found")
        result = reconcile_invoice(invoice.pk)
        self.stdout.write(result.code)
