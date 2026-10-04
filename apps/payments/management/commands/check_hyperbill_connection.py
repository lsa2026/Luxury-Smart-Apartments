"""Read-only deployment check for the isolated HyperBill API account."""

from django.core.management.base import BaseCommand, CommandError

from apps.payments.hyperbill import HyperBillClient, HyperBillError


class Command(BaseCommand):
    help = "Validate HyperBill sandbox API login, without invoices or messages."

    def handle(self, *args, **options):
        try:
            with HyperBillClient() as client:
                client.check_connection()
        except HyperBillError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write("HYPERBILL_SANDBOX_API_CONNECTED (no invoice or message created)")
