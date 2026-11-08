"""Phased Django client for isolated n8n contract tests; no live providers."""

import json
from contextlib import ExitStack
from unittest.mock import patch

from django.http import JsonResponse
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.payments.day_use_payments import create_checkout
from apps.payments.models import DayUsePaymentAttempt
from apps.reservations import day_use08_gateway, day_use_auto_payment, day_use_loyalty
from apps.reservations import day_use08_payments as service
from apps.reservations import day_use_payment_messages as links
from apps.reservations.day_use08_communications import delivery_token, event_token
from apps.reservations.day_use_hostaway import booking_state
from tests.day_use08_fixtures import confirmed, receipts
from tests.test_hyperpay import HyperPayStub
from tests.test_morning_stays import approved, consent
from tests.test_reservation_payment_links import billing


def start_payment(stay, hostaway, gateway):
    return create_checkout(
        stay.pk, billing_data=billing(), acceptance=consent(), client=gateway, hostaway=hostaway
    )


class PaymentHarness:
    """Use inside a disposable Django test DB, with DayUseStay test settings."""

    def __init__(self, *, full_journey=False):
        self.full_journey = full_journey
        self.event = None
        self.attempt = None

    def __enter__(self):
        self.stack = ExitStack()
        self.stack.__enter__()
        self.stack.enter_context(
            override_settings(
                ULTRAMSG_ENABLED=True,
                ULTRAMSG_INSTANCE_ID="synthetic",
                ULTRAMSG_TOKEN="synthetic",
                DAY_USE08_WORK_GROUP_ID="123456789@g.us",
            )
        )
        self.stack.enter_context(patch.object(service, "queue_event"))
        self.stack.enter_context(
            patch("apps.reservations.day_use08_communications.queue_stay_event")
        )
        self.stack.enter_context(patch.object(day_use_loyalty, "ready", return_value=True))
        self.stack.enter_context(
            patch.object(day_use_loyalty, "exact_receipt_messages", side_effect=self.fake_receipts)
        )
        self.stack.enter_context(
            patch.object(service, "exact_receipt_messages", side_effect=self.fake_receipts)
        )
        self.text_client = self.stack.enter_context(patch.object(service, "UltraMsgClient"))
        self.image_client = self.stack.enter_context(
            patch.object(day_use_loyalty, "UltraMsgClient")
        )
        self.text = self.text_client.return_value.__enter__.return_value
        self.image = self.image_client.return_value.__enter__.return_value
        self.text.send_text.return_value = {"sent": "true", "id": "synthetic-success"}
        self.image.send_image.return_value = {"sent": "true", "id": "synthetic-invitation"}
        self.core_client = self.stack.enter_context(
            patch.object(day_use08_gateway, "UltraMsgClient")
        )
        self.core = self.core_client.return_value.__enter__.return_value

        def accept_core(**values):
            return {"sent": "true", "id": "synthetic-" + values["reference_id"]}

        self.core.send_text.side_effect = accept_core
        self.core.send_image.side_effect = accept_core
        self.stack.enter_context(
            patch.object(
                day_use08_gateway, "exact_receipt_messages", side_effect=self.fake_receipts
            )
        )
        self.link_client = self.stack.enter_context(patch.object(links, "UltraMsgClient"))
        self.link = self.link_client.return_value.__enter__.return_value
        self.link.send_text.return_value = {"sent": "true", "id": "synthetic-payment-link"}
        self.stack.enter_context(patch.object(day_use_auto_payment, "dispatch"))
        self.acks = {"core": 1, "payment_success": 1, "loyalty_invitation": 1}
        self.stay, self.hostaway = confirmed()
        own = booking_state(self.stay)
        self.hostaway.guest_stay_page.return_value = [
            {
                "id": own["reservation_id"],
                "listingMapId": self.stay.property.hostaway_listing_map_id,
                "source": "LSA_DayUse",
                "status": "new",
                "arrivalDate": own["arrival_date"],
                "departureDate": own["departure_date"],
            }
        ]
        factory = self.stack.enter_context(patch("apps.reservations.day_use_stays.HostawayClient"))
        factory.return_value.__enter__.return_value = self.hostaway
        if not self.full_journey:
            self.initialize_paid()
        self.gateway = HyperPayStub()
        self.client = Client(enforce_csrf_checks=True)
        return self

    def initialize_paid(self):
        receipts(self.stay)
        self.attempt = DayUsePaymentAttempt.objects.create(
            stay=self.stay,
            merchant_transaction_id="synthetic-handoff-payment",
            provider_payment_id="synthetic-verified-payment",
            status="succeeded",
            amount=self.stay.amount_sar,
            currency="SAR",
            verified_at=timezone.now(),
        )
        self.stay.status, self.stay.paid_at = "paid", self.attempt.verified_at
        self.stay.save(update_fields=["status", "paid_at", "updated_at"])
        self.event = self.stay.payment_events.get()

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def fake_receipts(self, attempt):
        kind = "core"
        for name in service.KINDS if self.event else ():
            if attempt.reference_id == service.reference(self.event.pk, name)[1]:
                kind = name
        if attempt.reference_id.startswith("lsa-loyalty-invite:"):
            kind = "loyalty_invitation"
        return [
            {
                "id": attempt.provider_id,
                "referenceId": attempt.reference_id,
                "ack": self.acks[kind],
                "status": "sent",
            }
        ]

    def source(self, token=None):
        if not self.event:
            return JsonResponse({"code": "no_payment_event"}, status=409)
        return self.client.get(
            reverse("integrations:day_use08_payment_event", args=[self.event.pk]),
            HTTP_X_LSA_DAYUSE08_PAYMENT_TOKEN=token or service.capability(self.event),
        )

    def send(self, kind, *, token=None, body=None):
        if not self.event:
            return JsonResponse({"code": "no_payment_event", "sent": False}, status=409)
        key, ref = service.reference(self.event.pk, kind)
        return self.client.post(
            reverse("integrations:day_use08_payment_send"),
            body or {"message_type": kind, "job_key": key, "reference_id": ref},
            content_type="application/json",
            HTTP_X_LSA_DAYUSE08_PAYMENT_TOKEN=token
            or service.capability(self.event, delivery=True),
        )

    def receipt(self, kind):
        if not self.event:
            return JsonResponse({"code": "no_payment_event"}, status=409)
        return self.client.get(
            reverse("integrations:day_use08_payment_receipt"),
            {"reference_id": service.reference(self.event.pk, kind)[1]},
            HTTP_X_LSA_DAYUSE08_PAYMENT_TOKEN=service.capability(self.event, delivery=True),
        )

    def phase(self, command):
        """JSON-line API: source/send/receipt/acks/stats, synthetic identities only."""
        action = command["action"]
        if action in {"booking_source", "booking_send", "booking_receipt", "payment_link_request"}:
            auth = {"HTTP_X_LSA_DAYUSE08_TOKEN": delivery_token(self.stay)}
            if action == "booking_source":
                event = self.stay.communication_events.get(
                    revision=self.stay.communication_revision
                )
                response = self.client.get(
                    reverse("integrations:day_use08_event", args=[event.pk]),
                    HTTP_X_LSA_DAYUSE08_TOKEN=event_token(event),
                )
            elif action == "booking_send":
                response = self.client.post(
                    reverse("integrations:day_use08_send"),
                    command["job"],
                    content_type="application/json",
                    **auth,
                )
            elif action == "booking_receipt":
                response = self.client.get(
                    reverse("integrations:day_use08_receipt"),
                    {"reference_id": command["reference_id"]},
                    **auth,
                )
            else:
                response = self.client.post(
                    reverse("integrations:day_use08_payment_link"),
                    {"core_delivered": True},
                    content_type="application/json",
                    **auth,
                )
            return {"status": response.status_code, "body": json.loads(response.content)}
        if action == "payment_link_complete":
            delivery = self.stay.payment_link_deliveries.first()
            if not delivery:
                return {"code": "no_payment_link"}
            return {
                "code": day_use_auto_payment.complete(delivery.pk, self.stay.communication_revision)
            }
        if action == "verified_payment":
            from apps.payments.day_use_payments import verify_payment

            codes = {
                "success": "000.000.000",
                "failed": "800.100.152",
                "pending": "000.200.100",
                "cancelled": "100.396.101",
                "review": "000.400.100",
                "unknown": "unrecognized",
            }
            code = codes[command.get("outcome", "success")]
            if self.attempt is None:
                self.attempt = start_payment(self.stay, self.hostaway, self.gateway)
            self.gateway.payment_document = approved(self.attempt, result={"code": code})
            self.attempt = verify_payment(self.attempt.pk, client=self.gateway)
            self.stay.refresh_from_db()
            self.event = self.stay.payment_events.first()
            return {
                "payment_status": self.attempt.status,
                "stay_status": self.stay.status,
                "payment_event_id": str(self.event.pk) if self.event else None,
            }
        if action == "acks":
            for kind, ack in command["values"].items():
                if kind not in self.acks or type(ack) is not int or ack not in range(5):
                    raise ValueError("invalid_synthetic_receipt")
                self.acks[kind] = ack
            return {"ok": True}
        if action == "stats":
            return {
                "text_posts": self.text.send_text.call_count,
                "image_posts": self.image.send_image.call_count,
                "booking_events": self.stay.communication_events.count(),
                "payment_events": self.stay.payment_events.count(),
                "core_posts": self.core.send_text.call_count + self.core.send_image.call_count,
                "payment_link_posts": self.link.send_text.call_count,
                "hostaway_creates": self.hostaway.create_reservation_with_price_details.call_count,
                "checkout_posts": self.gateway.checkout_calls,
                "payment_checks": self.gateway.payment_calls,
            }
        response = (
            self.source()
            if action == "source"
            else {"send": self.send, "receipt": self.receipt}[action](command["message_type"])
        )
        return {"status": response.status_code, "body": json.loads(response.content)}
