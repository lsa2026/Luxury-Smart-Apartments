"""Legacy provider client and accounting renderers; accounting sends use email."""

from __future__ import annotations

import base64
import logging
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.utils.html import strip_tags

from apps.reservations.models import BookingModificationRequest, Reservation

logger = logging.getLogger(__name__)


class UltraMsgError(Exception):
    """Expected UltraMsg delivery failure without leaking provider details."""


class UltraMsgConfigurationError(UltraMsgError):
    """The private delivery integration is disabled or incomplete."""


class UltraMsgConnectionError(UltraMsgError):
    """UltraMsg could not be reached safely."""


class UltraMsgResponseError(UltraMsgError):
    """UltraMsg rejected or did not accept the requested message."""


class UltraMsgClient:
    """Small allowlisted client for the existing business WhatsApp integration."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        if (
            not settings.ULTRAMSG_ENABLED
            or not settings.ULTRAMSG_INSTANCE_ID
            or not settings.ULTRAMSG_TOKEN
        ):
            raise UltraMsgConfigurationError()
        self._owns_http = http is None
        self.http = http or httpx.Client(
            base_url=settings.ULTRAMSG_API_BASE_URL,
            timeout=httpx.Timeout(
                settings.ULTRAMSG_READ_TIMEOUT,
                connect=settings.ULTRAMSG_CONNECT_TIMEOUT,
            ),
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> UltraMsgClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def send_text(self, *, recipient: str, body: str, reference_id: str) -> dict[str, Any]:
        try:
            response = self.http.post(
                f"/{settings.ULTRAMSG_INSTANCE_ID}/messages/chat",
                data={
                    "token": settings.ULTRAMSG_TOKEN,
                    "to": recipient,
                    "body": body,
                    "priority": "1",
                    "referenceId": reference_id,
                },
            )
        except httpx.HTTPError as exc:
            raise UltraMsgConnectionError() from exc
        if response.status_code >= 500:
            raise UltraMsgConnectionError()
        if response.status_code >= 400:
            raise UltraMsgResponseError()
        try:
            document = response.json()
        except (TypeError, ValueError) as exc:
            raise UltraMsgResponseError() from exc
        if not isinstance(document, dict):
            raise UltraMsgResponseError()
        if str(document.get("sent", "")).casefold() in {"false", "0", "no"}:
            raise UltraMsgResponseError()
        if str(document.get("sent", "")).casefold() not in {"true", "1", "yes"}:
            # An ambiguous response is not proof of delivery and must not be retried blindly.
            raise UltraMsgConnectionError()
        return document

    def send_image(
        self, *, recipient: str, content: bytes, caption: str, reference_id: str
    ) -> dict[str, Any]:
        """Send one PNG with its caption; never silently truncate or retry as text."""
        encoded = base64.b64encode(content).decode("ascii")
        if (
            not content.startswith(b"\x89PNG\r\n\x1a\n")
            or len(encoded) > 10_000_000
            or len(caption.encode("utf-16-le")) // 2 > 1024
            or not caption.strip()
        ):
            raise UltraMsgResponseError()
        try:
            response = self.http.post(
                f"/{settings.ULTRAMSG_INSTANCE_ID}/messages/image",
                data={
                    "token": settings.ULTRAMSG_TOKEN,
                    "to": recipient,
                    "image": encoded,
                    "caption": caption,
                    "priority": "1",
                    "referenceId": reference_id,
                },
            )
        except httpx.HTTPError as exc:
            raise UltraMsgConnectionError() from exc
        if response.status_code >= 500:
            raise UltraMsgConnectionError()
        if response.status_code >= 400:
            raise UltraMsgResponseError()
        try:
            document = response.json()
        except (TypeError, ValueError) as exc:
            raise UltraMsgResponseError() from exc
        if not isinstance(document, dict):
            raise UltraMsgResponseError()
        sent = str(document.get("sent", "")).casefold()
        if sent in {"false", "0", "no"}:
            raise UltraMsgResponseError()
        if sent not in {"true", "1", "yes"}:
            raise UltraMsgConnectionError()
        return document

    def send_document(self, *, recipient: str, content: bytes, filename: str, caption: str) -> None:
        """Send a private PDF as base64; never expose a public guest-document URL."""
        encoded = base64.b64encode(content).decode("ascii")
        if not content.startswith(b"%PDF-") or len(encoded) > 10_000_000:
            raise UltraMsgResponseError()
        try:
            response = self.http.post(
                f"/{settings.ULTRAMSG_INSTANCE_ID}/messages/document",
                data={
                    "token": settings.ULTRAMSG_TOKEN,
                    "to": recipient,
                    "document": encoded,
                    "filename": filename,
                    "caption": caption[:1024],
                },
            )
        except httpx.HTTPError as exc:
            raise UltraMsgConnectionError() from exc
        if response.status_code >= 500:
            raise UltraMsgConnectionError()
        if response.status_code >= 400:
            raise UltraMsgResponseError()
        try:
            document = response.json()
        except (TypeError, ValueError) as exc:
            raise UltraMsgConnectionError() from exc
        if not isinstance(document, dict):
            raise UltraMsgConnectionError()
        sent = str(document.get("sent", "")).casefold()
        if sent in {"false", "0", "no"}:
            raise UltraMsgResponseError()
        if sent not in {"true", "1", "yes"}:
            raise UltraMsgConnectionError()


def send_manual_payment_link_request(*, reservation_id: object):
    """Compatibility entry point; manual-link alerts now use operations email."""
    from .accounting_email import send_manual_payment_link_request as send_email

    return send_email(reservation_id=reservation_id)


def send_modification_payment_link_request(*, modification_id: object):
    """Compatibility entry point; change-payment alerts use operations email."""
    from .accounting_email import send_modification_payment_link_request as send_email

    return send_email(modification_id=modification_id)


def _clean(value: object, *, limit: int = 180) -> str:
    return " ".join(strip_tags(str(value)).split())[:limit]


def _manual_payment_message(reservation: Reservation) -> str:
    intent = reservation.booking_intent
    guest_name = _clean(f"{intent.guest_first_name} {intent.guest_last_name}") or "غير مسجل"
    hostaway_id = reservation.hostaway_reservation_id
    hostaway_url = f"https://dashboard.hostaway.com/reservations/{hostaway_id}"
    total = Decimal(reservation.total_price).quantize(Decimal("0.01"))
    return "\n".join(
        (
            "طلب إنشاء رابط دفع يدوي",
            f"الضيف: {guest_name}",
            f"جوال الضيف: {_clean(intent.guest_phone, limit=40)}",
            f"البريد: {_clean(intent.guest_email, limit=254)}",
            f"الوحدة: {_clean(reservation.property)}",
            f"الإقامة: {reservation.check_in} إلى {reservation.check_out}",
            f"المبلغ: {total:,.2f} {reservation.currency}",
            f"مرجع الحجز في الموقع: {reservation.public_reference}",
            f"رقم حجز Hostaway: {hostaway_id}",
            f"رابط Hostaway: {hostaway_url}",
            "يرجى إنشاء رابط HyperPay اليدوي وإرساله للضيف بعد المراجعة.",
        )
    )


def _modification_payment_message(modification: BookingModificationRequest) -> str:
    reservation = modification.reservation
    intent = reservation.booking_intent
    guest_name = _clean(f"{intent.guest_first_name} {intent.guest_last_name}") or "غير مسجل"
    hostaway_id = reservation.hostaway_reservation_id
    hostaway_url = f"https://dashboard.hostaway.com/reservations/{hostaway_id}"
    difference = Decimal(modification.price_difference).quantize(Decimal("0.01"))
    if modification.quote_snapshot.get("owner_final_total") is not None:
        from apps.reservations.services.owner_settlement import settlement_for

        difference = settlement_for(reservation, modification.new_total).due.quantize(
            Decimal("0.01")
        )
    new_total = Decimal(modification.new_total or 0).quantize(Decimal("0.01"))
    return "\n".join(
        (
            "طلب إنشاء رابط دفع يدوي لفرق تعديل حجز",
            f"الضيف: {guest_name}",
            f"جوال الضيف: {_clean(intent.guest_phone, limit=40)}",
            f"البريد: {_clean(intent.guest_email, limit=254)}",
            f"الوحدة: {_clean(reservation.property)}",
            f"الفترة الجديدة: {modification.new_check_in} إلى {modification.new_check_out}",
            f"الإجمالي الجديد: {new_total:,.2f} {modification.currency}",
            f"المبلغ المتبقي المطلوب تحصيله: {difference:,.2f} {modification.currency}",
            f"مرجع التعديل: {modification.public_reference}",
            f"رقم حجز Hostaway: {hostaway_id}",
            f"رابط Hostaway: {hostaway_url}",
            "يرجى إنشاء رابط HyperPay اليدوي لفرق التعديل وإرساله للضيف بعد المراجعة.",
        )
    )
