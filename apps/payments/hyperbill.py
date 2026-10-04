"""HyperBill Simple Invoice sandbox integration per the official APIary contract."""

import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx
import phonenumbers
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.services.ultramsg import (
    UltraMsgClient,
    UltraMsgConfigurationError,
    UltraMsgConnectionError,
    UltraMsgResponseError,
)
from apps.reservations.models import Reservation

from .models import HyperBillInvoice


class HyperBillError(Exception):
    """Static codes only; no provider payload, password, token or PII."""


def _login_rejection(response):
    """Recognize only documented login failures; never expose response contents."""
    try:
        document = response.json()
        errors = document.get("errors", {}) if isinstance(document, dict) else {}
        detail = str(errors.get("email", "")).lower() if isinstance(errors, dict) else ""
    except ValueError:
        detail = ""
    if "wrong passowrd" in detail or "wrong password" in detail:
        return "hyperbill_login_wrong_password"
    if "unable to find user" in detail:
        return "hyperbill_login_user_not_found"
    return f"hyperbill_login_rejected_http_{response.status_code}"


def require_sandbox():
    if (
        not settings.HYPERBILL_ENABLED
        or settings.HYPERPAY_ENVIRONMENT != "test"
        or settings.HYPERBILL_BASE_URL != "https://hyperbill-sandbox.hyperpay.com"
        or settings.SITE_BASE_URL.rstrip("/") != "https://checkout-uat-v2.onrender.com"
    ):
        raise HyperBillError("hyperbill_disabled_or_not_uat")


class HyperBillClient:
    def __init__(self, http=None):
        require_sandbox()
        if not settings.HYPERBILL_EMAIL or not settings.HYPERBILL_PASSWORD:
            raise HyperBillError("hyperbill_credentials_missing")
        self.owns_http = http is None
        self.http = http or httpx.Client(
            base_url=settings.HYPERBILL_BASE_URL,
            timeout=httpx.Timeout(8, connect=4),
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        self.token = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.owns_http:
            self.http.close()

    def _request(self, method, path, *, payload=None, authenticated=True):
        if authenticated and not self.token:
            login = self._request(
                "POST",
                "/api/login",
                authenticated=False,
                payload={
                    "email": settings.HYPERBILL_EMAIL,
                    "password": settings.HYPERBILL_PASSWORD,
                },
            )
            data = login.get("data")
            token = data.get("accessToken") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise HyperBillError("hyperbill_login_rejected")
            self.token = token
        try:
            response = self.http.request(
                method,
                path,
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"} if authenticated else {},
            )
        except httpx.HTTPError:
            raise HyperBillError("hyperbill_outcome_unknown") from None
        if response.status_code >= 500:
            raise HyperBillError("hyperbill_outcome_unknown")
        if response.status_code >= 400 or response.is_redirect:
            if path == "/api/login":
                raise HyperBillError(_login_rejection(response))
            raise HyperBillError("hyperbill_request_rejected")
        try:
            document = response.json()
        except ValueError:
            raise HyperBillError("hyperbill_invalid_response") from None
        if not isinstance(document, dict) or document.get("status") is not True:
            if path == "/api/login":
                raise HyperBillError(_login_rejection(response))
            raise HyperBillError("hyperbill_request_rejected")
        return document

    def check_connection(self):
        """Validate login only, with no invoice, message or payment creation."""
        document = self._request(
            "POST",
            "/api/login",
            authenticated=False,
            payload={"email": settings.HYPERBILL_EMAIL, "password": settings.HYPERBILL_PASSWORD},
        )
        data = document.get("data")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("accessToken"), str)
            or not data["accessToken"]
        ):
            raise HyperBillError("hyperbill_login_rejected")
        return "connected"

    def create_invoice(self, payload):
        return self._request("POST", "/api/simpleInvoice", payload=payload)

    def retrieve_invoice(self, invoice_no):
        return self._request("GET", f"/api/simpleInvoice/retrieve/{quote(invoice_no, safe='')}")

    def retrieve_reference(self, reference):
        return self._request("GET", f"/api/simpleInvoice/retrieve/min/{quote(reference, safe='')}")


@dataclass(frozen=True)
class HyperBillOutcome:
    code: str
    invoice: HyperBillInvoice | None = None


def _phone(raw):
    try:
        number = phonenumbers.parse(raw, "SA")
        if not phonenumbers.is_valid_number(number):
            raise ValueError
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    except (phonenumbers.NumberParseException, ValueError):
        raise HyperBillError("guest_phone_invalid") from None


def _invoice_data(document):
    data = document.get("data")
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise HyperBillError("hyperbill_invoice_missing_or_ambiguous")
    return data


def _validate_invoice(invoice, data):
    try:
        amount = Decimal(str(data.get("amount")))
    except InvalidOperation:
        raise HyperBillError("hyperbill_amount_mismatch") from None
    if not amount.is_finite() or amount != invoice.amount:
        raise HyperBillError("hyperbill_amount_mismatch")
    if (
        data.get("currency") != invoice.currency
        or data.get("merchant_invoice_number") != invoice.merchant_reference
        or data.get("payment_type") != "DB"
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", str(data.get("invoice_no", "")))
        or (invoice.invoice_no and data.get("invoice_no") != invoice.invoice_no)
    ):
        raise HyperBillError("hyperbill_invoice_mismatch")


def _save_remote_invoice(invoice_id, document, *, verified=False):
    data = _invoice_data(document)
    with transaction.atomic():
        invoice = HyperBillInvoice.objects.select_for_update().get(pk=invoice_id)
        _validate_invoice(invoice, data)
        invoice.invoice_no = data["invoice_no"]
        # Documented canonical sandbox URL; don't trust returned arbitrary/production links.
        invoice.payment_url = (
            f"{settings.HYPERBILL_BASE_URL}/invoice/show/simple/{invoice.invoice_no}"
        )
        remote_status = data.get("status", "pending")
        if verified and remote_status not in {"paid", "pending", "canceled", "declined"}:
            raise HyperBillError("hyperbill_status_unknown")
        if invoice.status != "paid":
            invoice.status = (
                remote_status
                if verified and remote_status in {"paid", "pending", "canceled", "declined"}
                else "pending"
            )
        if verified:
            invoice.verified_at = timezone.now()
        invoice.error_code = ""
        invoice.save()
        # No real PaymentAttempt, Hostaway paid charge, refund or purchase event
        # may be derived from a sandbox collection.
        return invoice


def create_guest_payment_link(*, reservation_id):
    require_sandbox()
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=reservation_id)
        intent = reservation.booking_intent
        if (
            intent is None
            or not reservation.hostaway_reservation_id
            or reservation.normalized_status not in Reservation.ACTIVE_STATUSES
            or reservation.payment_status not in {"unpaid", "awaiting_payment", "pending", ""}
            or reservation.total_price != intent.total_price
            or reservation.currency != intent.currency
        ):
            return HyperBillOutcome("confirmed_unpaid_hostaway_booking_required")
        recipient = _phone(intent.guest_phone)
        if recipient not in settings.HYPERBILL_WHATSAPP_ALLOWED_NUMBERS:
            return HyperBillOutcome("sandbox_guest_not_allowlisted")
        amount = Decimal(str(intent.payment_amount_sar or 0))
        if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
            return HyperBillOutcome("final_sar_price_required")
        invoice, created = HyperBillInvoice.objects.get_or_create(
            reservation=reservation,
            defaults={
                "merchant_reference": f"HB{uuid.uuid4().hex[:14]}",
                "amount": amount,
                "currency": "SAR",
                "expires_at": min(intent.expires_at, timezone.now() + timedelta(hours=24)),
            },
        )
        if invoice.amount != amount:
            return HyperBillOutcome("price_changed_requires_review", invoice)
        if invoice.expires_at <= timezone.now():
            return HyperBillOutcome("invoice_expired", invoice)
        if not created:
            if invoice.status in {"creating", "unknown", "review"}:
                return HyperBillOutcome("invoice_requires_reconciliation", invoice)
            if invoice.status != "pending":
                return HyperBillOutcome("invoice_not_payable", invoice)
    if created:
        payload = {
            "amount": f"{invoice.amount:.2f}",
            "currency": "SAR",
            "payment_type": "DB",
            "name": f"{intent.guest_first_name} {intent.guest_last_name}".strip(),
            "email": intent.guest_email,
            "phone": recipient.lstrip("+"),
            "lang": "ar" if intent.language.startswith("ar") else "en",
            "merchant_invoice_number": invoice.merchant_reference,
            "expiration_date": invoice.expires_at.astimezone(
                timezone.get_default_timezone()
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            with HyperBillClient() as client:
                document = client.create_invoice(payload)
            invoice = _save_remote_invoice(invoice.pk, document)
        except HyperBillError as exc:
            HyperBillInvoice.objects.filter(pk=invoice.pk).update(
                status="unknown", error_code=str(exc), updated_at=timezone.now()
            )
            return HyperBillOutcome(str(exc), invoice)
    return send_guest_payment_link(invoice.pk)


def send_guest_payment_link(invoice_id):
    require_sandbox()
    with transaction.atomic():
        invoice = HyperBillInvoice.objects.select_for_update().get(pk=invoice_id)
        reservation = invoice.reservation
        intent = reservation.booking_intent
        if (
            invoice.status != "pending"
            or not invoice.payment_url
            or invoice.expires_at <= timezone.now()
            or reservation.normalized_status not in Reservation.ACTIVE_STATUSES
            or intent is None
            or intent.expires_at <= timezone.now()
            or reservation.payment_status not in {"unpaid", "awaiting_payment", "pending", ""}
            or reservation.total_price != intent.total_price
            or reservation.currency != intent.currency
            or invoice.amount != intent.payment_amount_sar
        ):
            return HyperBillOutcome("invoice_not_payable", invoice)
        if invoice.delivery_status != "not_sent":
            return HyperBillOutcome(
                "already_sent" if invoice.delivery_status == "sent" else "delivery_requires_review",
                invoice,
            )
        recipient = _phone(reservation.booking_intent.guest_phone)
        if recipient not in settings.HYPERBILL_WHATSAPP_ALLOWED_NUMBERS:
            return HyperBillOutcome("sandbox_guest_not_allowlisted", invoice)
        invoice.delivery_status = "sending"
        invoice.save(update_fields=["delivery_status", "updated_at"])
    body = "\n".join(
        (
            "رابط دفع تجريبي — لا يمثل تحصيلًا فعليًا",
            f"مرجع الحجز: {reservation.public_reference}",
            f"الإقامة: {reservation.check_in} إلى {reservation.check_out}",
            f"المبلغ النهائي: {invoice.amount:.2f} SAR",
            invoice.payment_url,
        )
    )
    try:
        with UltraMsgClient() as client:
            response = client.send_text(
                recipient=recipient, body=body, reference_id=f"hyperbill:{invoice.pk}"
            )
    except UltraMsgConfigurationError:
        # No request was made: unlike a network timeout, retry after setup is safe.
        HyperBillInvoice.objects.filter(pk=invoice.pk).update(
            delivery_status="not_sent",
            error_code="guest_whatsapp_configuration_missing",
            updated_at=timezone.now(),
        )
        return HyperBillOutcome("guest_whatsapp_configuration_missing", invoice)
    except (UltraMsgConnectionError, UltraMsgResponseError):
        HyperBillInvoice.objects.filter(pk=invoice.pk).update(
            delivery_status="unknown",
            error_code="guest_whatsapp_requires_review",
            updated_at=timezone.now(),
        )
        return HyperBillOutcome("guest_whatsapp_requires_review", invoice)
    HyperBillInvoice.objects.filter(pk=invoice.pk).update(
        delivery_status="sent",
        delivery_message_id=str(response.get("id", ""))[:255],
        sent_at=timezone.now(),
        error_code="",
        updated_at=timezone.now(),
    )
    invoice.refresh_from_db()
    return HyperBillOutcome("sent", invoice)


def reconcile_invoice(invoice_id, *, client=None):
    require_sandbox()
    invoice = HyperBillInvoice.objects.get(pk=invoice_id)
    if invoice.status == "paid":
        return HyperBillOutcome("paid", invoice)
    try:
        if client is None:
            with HyperBillClient() as owned:
                return reconcile_invoice(invoice_id, client=owned)
        document = (
            client.retrieve_invoice(invoice.invoice_no)
            if invoice.invoice_no
            else client.retrieve_reference(invoice.merchant_reference)
        )
        invoice = _save_remote_invoice(invoice.pk, document, verified=True)
        return HyperBillOutcome(invoice.status, invoice)
    except HyperBillError as exc:
        HyperBillInvoice.objects.filter(pk=invoice.pk).exclude(status="paid").update(
            error_code=str(exc), updated_at=timezone.now()
        )
        return HyperBillOutcome(str(exc), invoice)
