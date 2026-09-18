"""UltraMsg delivery for the accountant's manual-payment-link workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.html import strip_tags

from apps.notifications.models import WhatsAppDelivery
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
    """Small allowlisted client for UltraMsg's text-message endpoint."""

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
        return document


@dataclass(frozen=True, slots=True)
class WhatsAppDeliveryResult:
    code: str
    delivery: WhatsAppDelivery


def send_manual_payment_link_request(*, reservation_id: object) -> WhatsAppDeliveryResult:
    """Send exactly one accountant alert after a manual Hostaway booking.

    A timeout is intentionally not retried automatically: UltraMsg may have
    accepted the message even when its response is lost.  This prevents a
    second request from reaching the accountant for the same reservation.
    """

    with transaction.atomic():
        reservation = (
            Reservation.objects.select_for_update()
            .filter(pk=reservation_id)
            .first()
        )
        if reservation is None:
            raise ValueError("Reservation was not found.")
        delivery, created = WhatsAppDelivery.objects.get_or_create(
            reservation=reservation,
            modification_request=None,
            defaults={
                "message_type": "manual_payment_link_request",
                "recipient_masked": _mask_phone(settings.ACCOUNTING_WHATSAPP_NUMBER),
                "recipient_reference": "accounting",
                "provider": "ultramsg",
                "idempotency_key": f"manual-payment-link:{reservation.public_reference}",
                "status": (
                    WhatsAppDelivery.Status.QUEUED
                    if settings.ULTRAMSG_ENABLED
                    else WhatsAppDelivery.Status.DISABLED
                ),
                "queued_at": timezone.now(),
                "last_error_code": "" if settings.ULTRAMSG_ENABLED else "ultramsg_disabled",
            },
        )
        if not created:
            if delivery.status == WhatsAppDelivery.Status.SENT:
                return WhatsAppDeliveryResult("already_sent", delivery)
            if delivery.status == WhatsAppDelivery.Status.FAILED:
                return WhatsAppDeliveryResult("previously_failed", delivery)
            if delivery.status == WhatsAppDelivery.Status.DISABLED:
                return WhatsAppDeliveryResult("disabled", delivery)
            return WhatsAppDeliveryResult("already_requested", delivery)
        if delivery.status == WhatsAppDelivery.Status.DISABLED:
            return WhatsAppDeliveryResult("disabled", delivery)
        delivery.status = WhatsAppDelivery.Status.SENDING
        delivery.attempt_count = 1
        delivery.save(update_fields=["status", "attempt_count", "updated_at"])

    try:
        body = _manual_payment_message(reservation)
    except Exception:
        # The booking is already authoritative in Hostaway. Keep the delivery
        # row auditable and return an operational failure instead of turning a
        # successful booking into a generic HTTP 500 page.
        logger.exception(
            "Could not build the UltraMsg manual-payment message for reservation %s.",
            reservation.pk,
        )
        return _finish_failure(delivery.pk, "failed", "message_build_failed")

    return _send_delivery(delivery=delivery, body=body)


def send_modification_payment_link_request(*, modification_id: object) -> WhatsAppDeliveryResult:
    """Ask accounting for a manual link when a confirmed change costs more."""

    with transaction.atomic():
        modification = (
            BookingModificationRequest.objects.select_for_update()
            .select_related("reservation", "reservation__booking_intent", "reservation__property")
            .filter(pk=modification_id)
            .first()
        )
        if modification is None:
            raise ValueError("Modification request was not found.")
        if modification.price_difference <= 0:
            raise ValueError("A payment link is only needed for a positive difference.")
        delivery, created = WhatsAppDelivery.objects.get_or_create(
            modification_request=modification,
            defaults={
                "message_type": "modification_payment_link_request",
                "recipient_masked": _mask_phone(settings.ACCOUNTING_WHATSAPP_NUMBER),
                "recipient_reference": "accounting",
                "provider": "ultramsg",
                "idempotency_key": f"modification-payment-link:{modification.public_reference}",
                "status": (
                    WhatsAppDelivery.Status.QUEUED
                    if settings.ULTRAMSG_ENABLED
                    else WhatsAppDelivery.Status.DISABLED
                ),
                "queued_at": timezone.now(),
                "last_error_code": "" if settings.ULTRAMSG_ENABLED else "ultramsg_disabled",
            },
        )
        if not created:
            if delivery.status == WhatsAppDelivery.Status.SENT:
                return WhatsAppDeliveryResult("already_sent", delivery)
            if delivery.status == WhatsAppDelivery.Status.FAILED:
                return WhatsAppDeliveryResult("previously_failed", delivery)
            if delivery.status == WhatsAppDelivery.Status.DISABLED:
                return WhatsAppDeliveryResult("disabled", delivery)
            return WhatsAppDeliveryResult("already_requested", delivery)
        if delivery.status == WhatsAppDelivery.Status.DISABLED:
            return WhatsAppDeliveryResult("disabled", delivery)
        delivery.status = WhatsAppDelivery.Status.SENDING
        delivery.attempt_count = 1
        delivery.save(update_fields=["status", "attempt_count", "updated_at"])

    return _send_delivery(
        delivery=delivery,
        body=_modification_payment_message(modification),
    )


def _send_delivery(*, delivery: WhatsAppDelivery, body: str) -> WhatsAppDeliveryResult:
    try:
        with UltraMsgClient() as client:
            response = client.send_text(
                recipient=_accounting_recipient(),
                body=body,
                reference_id=delivery.idempotency_key,
            )
    except UltraMsgConfigurationError:
        return _finish_failure(delivery.pk, "disabled", "ultramsg_disabled")
    except UltraMsgConnectionError:
        return _finish_failure(delivery.pk, "failed", "ultramsg_connection_failed")
    except UltraMsgResponseError:
        return _finish_failure(delivery.pk, "failed", "ultramsg_request_rejected")

    with transaction.atomic():
        delivery = WhatsAppDelivery.objects.select_for_update().get(pk=delivery.pk)
        delivery.status = WhatsAppDelivery.Status.SENT
        provider_message_id = response.get("id") or response.get("message_id") or ""
        delivery.provider_message_id = str(provider_message_id)[:255]
        delivery.last_error_code = ""
        delivery.sent_at = timezone.now()
        delivery.save(
            update_fields=[
                "status",
                "provider_message_id",
                "last_error_code",
                "sent_at",
                "updated_at",
            ]
        )
    return WhatsAppDeliveryResult("sent", delivery)


def _finish_failure(
    delivery_id: object,
    code: str,
    error_code: str,
) -> WhatsAppDeliveryResult:
    with transaction.atomic():
        delivery = WhatsAppDelivery.objects.select_for_update().get(pk=delivery_id)
        delivery.status = (
            WhatsAppDelivery.Status.DISABLED
            if code == "disabled"
            else WhatsAppDelivery.Status.FAILED
        )
        delivery.last_error_code = error_code
        delivery.failed_at = timezone.now()
        delivery.save(update_fields=["status", "last_error_code", "failed_at", "updated_at"])
    return WhatsAppDeliveryResult(code, delivery)


def _accounting_recipient() -> str:
    digits = "".join(
        character
        for character in settings.ACCOUNTING_WHATSAPP_NUMBER
        if character.isdigit()
    )
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = f"{settings.WHATSAPP_DEFAULT_COUNTRY_CODE}{digits.lstrip('0')}"
    elif not digits.startswith(settings.WHATSAPP_DEFAULT_COUNTRY_CODE) and len(digits) <= 9:
        digits = f"{settings.WHATSAPP_DEFAULT_COUNTRY_CODE}{digits}"
    if not digits:
        raise UltraMsgConfigurationError()
    return f"+{digits}"


def _mask_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    return f"••••{digits[-4:]}" if digits else "غير مضبوط"


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
            f"فرق الزيادة المطلوب تحصيله: {difference:,.2f} {modification.currency}",
            f"مرجع التعديل: {modification.public_reference}",
            f"رقم حجز Hostaway: {hostaway_id}",
            f"رابط Hostaway: {hostaway_url}",
            "يرجى إنشاء رابط HyperPay اليدوي لفرق التعديل وإرساله للضيف بعد المراجعة.",
        )
    )
