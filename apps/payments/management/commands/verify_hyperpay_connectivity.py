"""Prove the configured HyperPay credentials work against a chosen environment.

Creating a checkout is HyperPay's own connectivity probe: it opens a payment
session but moves no money, and nothing is charged unless a cardholder later
completes it. The command never prints the access token.
"""

from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser


class Command(BaseCommand):
    help = "Check HyperPay credentials against the TEST or production endpoint."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--environment",
            choices=sorted(settings.HYPERPAY_APPROVED_BASE_URLS),
            default=settings.HYPERPAY_ENVIRONMENT,
            help="Endpoint to probe. Defaults to the configured environment.",
        )
        parser.add_argument("--amount", default="1.00")

    def handle(self, *args: object, **options: Any) -> None:
        environment = options["environment"]
        base_url = settings.HYPERPAY_APPROVED_BASE_URLS[environment]
        entity_id = settings.HYPERPAY_ENTITY_ID
        token = settings.HYPERPAY_ACCESS_TOKEN

        if not entity_id or not token:
            raise CommandError("HYPERPAY_ENTITY_ID and HYPERPAY_ACCESS_TOKEN must both be set.")

        self.stdout.write(f"Endpoint    : {base_url}")
        self.stdout.write(f"Environment : {environment}")
        self.stdout.write(f"Entity ID   : {entity_id[:6]}...{entity_id[-4:]}")
        self.stdout.write(f"Token       : {len(token)} characters (not shown)")
        self.stdout.write("")

        payload = {
            "entityId": entity_id,
            "amount": str(Decimal(options["amount"]).quantize(Decimal("0.01"))),
            "currency": settings.HYPERPAY_CURRENCY,
            "paymentType": settings.HYPERPAY_PAYMENT_TYPE,
            "merchantTransactionId": "connectivity-probe",
        }

        try:
            response = httpx.post(
                f"{base_url.rstrip('/')}/v1/checkouts",
                data=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(20.0, connect=5.0),
            )
        except httpx.HTTPError as exc:
            raise CommandError(f"Could not reach {base_url}: {exc}") from exc

        self.stdout.write(f"HTTP status : {response.status_code}")

        if response.status_code in {401, 403}:
            raise CommandError(
                "Authentication rejected. The token does not belong to this "
                f"{environment} endpoint, or it is not authorised for this entity."
            )

        try:
            document = response.json()
        except ValueError as exc:
            raise CommandError("HyperPay returned a non-JSON response.") from exc

        result = document.get("result") or {}
        code = result.get("code", "")
        description = result.get("description", "")
        self.stdout.write(f"Result code : {code}")
        self.stdout.write(f"Description : {description}")
        self.stdout.write(f"Checkout ID : {'issued' if document.get('id') else 'none'}")
        self.stdout.write("")

        if code.startswith("000.200."):
            self.stdout.write(
                self.style.SUCCESS(f"OK - credentials are valid for the {environment} endpoint.")
            )
            return
        raise CommandError(f"HyperPay rejected the probe: {code} {description}")
