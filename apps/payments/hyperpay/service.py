"""HyperPay business integration over the provider-neutral payment ledger."""

import logging
import re
import secrets
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.payments.currency import (
    PAYMENT_CURRENCY,
    CurrencyError,
    validate_payment_snapshot,
)
from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingIntent, BookingModificationRequest, Reservation
from apps.reservations.services.automatic_modifications import (
    AutomaticModificationOutcome,
    execute_automatic_modification,
)
from apps.reservations.services.availability import AvailabilityRequest, AvailabilityService
from apps.reservations.services.hostaway_booking import (
    HostawayBookingService,
    ReservationCreationOutcome,
    prepare_local_reservation,
)
from apps.reservations.services.modifications import ModificationService

from ..countries import normalize_country_code
from .client import HyperPayClient
from .exceptions import HyperPayCheckoutError, HyperPayVerificationError
from .result_codes import HyperPayStatus, map_result_code

logger = logging.getLogger(__name__)
HYPERPAY_PROVIDER = "hyperpay"


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    attempt: PaymentAttempt
    checkout_id: str
    script_integrity: str


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    attempt: PaymentAttempt
    status: HyperPayStatus
    reservation: Reservation | None = None
    hostaway: ReservationCreationOutcome | None = None
    modification: BookingModificationRequest | None = None
    automatic_modification: AutomaticModificationOutcome | None = None


def format_test_amount(amount: Decimal) -> str:
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
        raise ValueError("invalid_amount")
    normalized = amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if normalized != amount:
        raise ValueError("test_amount_requires_whole_sar")
    return format(normalized, ".2f")


def format_hyperpay_amount(amount: Decimal) -> str:
    if settings.HYPERPAY_ENVIRONMENT == "test":
        return format_test_amount(amount)
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
        raise ValueError("invalid_amount")
    normalized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if normalized != amount:
        raise ValueError("amount_has_unsupported_precision")
    return format(normalized, ".2f")


def merchant_transaction_id() -> str:
    return f"LSA-{secrets.token_hex(16)}"


def build_checkout_payload(
    intent: BookingIntent,
    merchant_id: str,
    *,
    amount: Decimal | None = None,
    currency: str | None = None,
) -> dict[str, str]:
    country = normalize_country_code(intent.billing_country)
    checkout_amount = intent.payment_amount_sar if amount is None else amount
    checkout_currency = PAYMENT_CURRENCY if currency is None else currency.upper()
    if checkout_amount is None:
        raise ValueError("payment_snapshot_missing")
    if checkout_currency != settings.HYPERPAY_CURRENCY:
        raise ValueError("currency_not_supported")
    required = {
        "customer.email": intent.guest_email.strip(),
        "customer.givenName": intent.guest_first_name.strip(),
        "customer.surname": intent.guest_last_name.strip(),
        "billing.street1": intent.billing_street1.strip(),
        "billing.city": intent.billing_city.strip(),
        "billing.state": intent.billing_state.strip(),
        "billing.postcode": intent.billing_postcode.strip(),
    }
    if not all(required.values()):
        raise ValueError("required_billing_data_missing")
    payload = {
        "entityId": settings.HYPERPAY_ENTITY_ID,
        "amount": format_hyperpay_amount(checkout_amount),
        "currency": settings.HYPERPAY_CURRENCY,
        "paymentType": settings.HYPERPAY_PAYMENT_TYPE,
        "merchantTransactionId": merchant_id,
        "integrity": "true",
        "billing.country": country,
        **required,
    }
    if settings.HYPERPAY_ENVIRONMENT == "test":
        payload.update(
            {
                "testMode": "EXTERNAL",
                "customParameters[3DS2_enrolled]": "true",
            }
        )
    return payload


class HyperPayService:
    def __init__(
        self,
        client: HyperPayClient | None = None,
        *,
        availability_service: AvailabilityService | None = None,
        modification_service: ModificationService | None = None,
    ) -> None:
        self.client = client or HyperPayClient()
        self._owns_client = client is None
        self.availability_service = availability_service
        self.modification_service = modification_service

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "HyperPayService":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def create_checkout(self, intent: BookingIntent) -> CheckoutSession:
        if (
            settings.HOSTAWAY_LIVE_BOOKING_ENABLED
            and intent.property.hostaway_listing_map_id is None
        ):
            # Never collect money for inventory that cannot yet be written to
            # the channel manager. The periodic reconciliation task normally
            # fills this verified identifier before a property is published.
            raise HyperPayCheckoutError("listing_map_id_not_verified")
        if settings.HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED:
            self._revalidate_booking_intent(intent)
        with transaction.atomic():
            locked = BookingIntent.objects.select_for_update().get(pk=intent.pk)
            if (
                locked.status != BookingIntent.Status.AWAITING_PAYMENT
                or locked.expires_at <= timezone.now()
            ):
                raise HyperPayCheckoutError("booking_intent_not_payable")
            try:
                validate_payment_snapshot(
                    source_amount=locked.total_price,
                    source_currency=locked.currency,
                    payment_amount_sar=locked.payment_amount_sar,
                    snapshot=locked.exchange_rate_snapshot,
                )
            except CurrencyError as exc:
                raise HyperPayCheckoutError("payment_snapshot_invalid") from exc
            existing = (
                PaymentAttempt.objects.select_for_update()
                .filter(
                    booking_intent=locked,
                    provider=HYPERPAY_PROVIDER,
                    status__in=[PaymentAttempt.Status.CREATED, PaymentAttempt.Status.PENDING],
                )
                .exclude(provider_checkout_id="")
                .order_by("-created_at")
                .first()
            )
            if existing and existing.provider_checkout_id and existing.widget_integrity:
                if (
                    existing.amount != locked.payment_amount_sar
                    or existing.currency != PAYMENT_CURRENCY
                ):
                    raise HyperPayCheckoutError("existing_payment_snapshot_mismatch")
                return CheckoutSession(
                    existing,
                    existing.provider_checkout_id,
                    existing.widget_integrity,
                )

            merchant_id = merchant_transaction_id()
            attempt = PaymentAttempt.objects.create(
                booking_intent=locked,
                provider=HYPERPAY_PROVIDER,
                merchant_transaction_id=merchant_id,
                amount=locked.payment_amount_sar,
                currency=PAYMENT_CURRENCY,
                status=PaymentAttempt.Status.CREATED,
                idempotency_key=secrets.token_urlsafe(32),
            )
            try:
                payload = build_checkout_payload(locked, merchant_id)
            except ValueError as exc:
                raise HyperPayCheckoutError(str(exc)) from exc
            response = self.client.create_checkout(payload)
            checkout_id = response.get("id")
            integrity = response.get("integrity")
            result = response.get("result")
            result_code = result.get("code") if isinstance(result, dict) else ""
            if (
                not isinstance(checkout_id, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{8,255}", checkout_id)
                or not isinstance(integrity, str)
                or not re.fullmatch(r"sha(?:256|384|512)-[A-Za-z0-9+/=]+", integrity)
                or map_result_code(result_code) is not HyperPayStatus.PENDING
            ):
                attempt.status = PaymentAttempt.Status.FAILED
                attempt.failure_code = "checkout_response_invalid"
                attempt.provider_result_code = result_code if isinstance(result_code, str) else ""
                attempt.save(
                    update_fields=["status", "failure_code", "provider_result_code", "updated_at"]
                )
                raise HyperPayCheckoutError()
            attempt.provider_checkout_id = checkout_id
            attempt.provider_reference = checkout_id
            attempt.widget_integrity = integrity
            attempt.provider_result_code = result_code
            attempt.status = PaymentAttempt.Status.PENDING
            attempt.save(
                update_fields=[
                    "provider_checkout_id",
                    "provider_reference",
                    "widget_integrity",
                    "provider_result_code",
                    "status",
                    "updated_at",
                ]
            )
        logger.info(
            "Payment checkout created: provider=hyperpay internal_payment_id=%s "
            "booking_intent_id=%s merchant_transaction_id=%s checkout_id=%s internal_status=%s",
            attempt.pk,
            attempt.booking_intent_id,
            attempt.merchant_transaction_id,
            checkout_id,
            attempt.status,
        )
        return CheckoutSession(attempt, checkout_id, integrity)

    def create_modification_checkout(
        self,
        modification: BookingModificationRequest,
    ) -> CheckoutSession:
        self._revalidate_modification(modification)
        with transaction.atomic():
            locked = (
                BookingModificationRequest.objects.select_for_update()
                .select_related("reservation__booking_intent")
                .get(pk=modification.pk)
            )
            intent = locked.reservation.booking_intent
            if (
                locked.status != BookingModificationRequest.Status.AWAITING_PAYMENT
                or locked.expires_at <= timezone.now()
                or locked.price_difference <= 0
                or intent is None
                or locked.payment_amount_sar is None
            ):
                raise HyperPayCheckoutError("modification_not_payable")
            try:
                validate_payment_snapshot(
                    source_amount=locked.price_difference,
                    source_currency=locked.currency,
                    payment_amount_sar=locked.payment_amount_sar,
                    snapshot=locked.quote_snapshot.get("fx"),
                )
            except CurrencyError as exc:
                raise HyperPayCheckoutError("payment_snapshot_invalid") from exc
            existing = (
                PaymentAttempt.objects.select_for_update()
                .filter(
                    modification_request=locked,
                    provider=HYPERPAY_PROVIDER,
                    status__in=[PaymentAttempt.Status.CREATED, PaymentAttempt.Status.PENDING],
                )
                .exclude(provider_checkout_id="")
                .order_by("-created_at")
                .first()
            )
            if existing and existing.provider_checkout_id and existing.widget_integrity:
                if (
                    existing.amount != locked.payment_amount_sar
                    or existing.currency != PAYMENT_CURRENCY
                ):
                    raise HyperPayCheckoutError("existing_payment_snapshot_mismatch")
                return CheckoutSession(
                    existing,
                    existing.provider_checkout_id,
                    existing.widget_integrity,
                )

            merchant_id = merchant_transaction_id()
            attempt = PaymentAttempt.objects.create(
                booking_intent=intent,
                modification_request=locked,
                provider=HYPERPAY_PROVIDER,
                merchant_transaction_id=merchant_id,
                amount=locked.payment_amount_sar,
                currency=PAYMENT_CURRENCY,
                status=PaymentAttempt.Status.CREATED,
                idempotency_key=secrets.token_urlsafe(32),
            )
            try:
                payload = build_checkout_payload(
                    intent,
                    merchant_id,
                    amount=locked.payment_amount_sar,
                    currency=PAYMENT_CURRENCY,
                )
            except ValueError as exc:
                raise HyperPayCheckoutError(str(exc)) from exc
            response = self.client.create_checkout(payload)
            checkout_id, integrity, _ = self._validate_checkout_response(
                response,
                attempt,
            )
        self._log_checkout(attempt, checkout_id)
        return CheckoutSession(attempt, checkout_id, integrity)

    def verify(self, attempt: PaymentAttempt) -> VerificationOutcome:
        if attempt.provider != HYPERPAY_PROVIDER or not attempt.provider_checkout_id:
            raise HyperPayVerificationError("invalid_payment_reference")
        attempt.refresh_from_db()
        if attempt.status == PaymentAttempt.Status.SUCCEEDED and attempt.verified_at:
            return self._complete_success(attempt)
        document = self.client.get_checkout_payment(attempt.provider_checkout_id)
        return self._process_verification(attempt.pk, document)

    def _process_verification(
        self,
        attempt_id: object,
        document: dict[str, Any],
    ) -> VerificationOutcome:
        result = document.get("result")
        code = result.get("code") if isinstance(result, dict) else ""
        description = result.get("description") if isinstance(result, dict) else ""
        mapped = map_result_code(code)
        with transaction.atomic():
            attempt = (
                PaymentAttempt.objects.select_for_update()
                .select_related("booking_intent__property")
                .get(pk=attempt_id)
            )
            if attempt.status == PaymentAttempt.Status.SUCCEEDED and attempt.verified_at:
                reservation = Reservation.objects.filter(
                    booking_intent=attempt.booking_intent
                ).first()
                return VerificationOutcome(attempt, HyperPayStatus.SUCCESS, reservation)
            mismatch = self._verification_mismatch(attempt, document)
            if mismatch:
                mapped = HyperPayStatus.REVIEW
                attempt.failure_code = mismatch
            attempt.provider_payment_id = _safe_optional_text(document.get("id"), 255)
            attempt.provider_result_code = _safe_text(code, 100)
            attempt.provider_result_description = _safe_text(description, 255)
            attempt.verified_at = timezone.now()
            attempt.status = {
                HyperPayStatus.SUCCESS: PaymentAttempt.Status.SUCCEEDED,
                HyperPayStatus.PENDING: PaymentAttempt.Status.PENDING,
                HyperPayStatus.FAILED: PaymentAttempt.Status.FAILED,
                HyperPayStatus.CANCELLED: PaymentAttempt.Status.CANCELLED,
                HyperPayStatus.REVIEW: PaymentAttempt.Status.REVIEW,
                HyperPayStatus.UNKNOWN: PaymentAttempt.Status.UNKNOWN,
            }[mapped]
            attempt.save()
        logger.info(
            "Payment verified: provider=hyperpay internal_payment_id=%s booking_intent_id=%s "
            "merchant_transaction_id=%s checkout_id=%s provider_payment_id=%s "
            "result_code=%s internal_status=%s",
            attempt.pk,
            attempt.booking_intent_id,
            attempt.merchant_transaction_id,
            attempt.provider_checkout_id,
            attempt.provider_payment_id,
            attempt.provider_result_code,
            attempt.status,
        )
        if mapped is not HyperPayStatus.SUCCESS:
            return VerificationOutcome(attempt, mapped)

        return self._complete_success(attempt)

    def _complete_success(self, attempt: PaymentAttempt) -> VerificationOutcome:
        if attempt.modification_request_id:
            modification = BookingModificationRequest.objects.select_related(
                "reservation"
            ).get(pk=attempt.modification_request_id)
            if modification.status == BookingModificationRequest.Status.AWAITING_PAYMENT:
                try:
                    # The checkout was revalidated before payment; repeat the
                    # check after 3DS and immediately before the Hostaway write.
                    self._revalidate_modification(modification)
                except HyperPayCheckoutError:
                    modification.refresh_from_db()
                    if (
                        modification.status
                        == BookingModificationRequest.Status.AWAITING_PAYMENT
                    ):
                        BookingModificationRequest.objects.filter(
                            pk=modification.pk,
                            status=BookingModificationRequest.Status.AWAITING_PAYMENT,
                        ).update(
                            status=(
                                BookingModificationRequest.Status.PENDING_ADMIN_APPROVAL
                            ),
                            updated_at=timezone.now(),
                        )
                        modification.refresh_from_db()
                    automatic = AutomaticModificationOutcome(
                        "postpayment_revalidation_failed",
                        modification,
                    )
                    return VerificationOutcome(
                        attempt,
                        HyperPayStatus.SUCCESS,
                        reservation=modification.reservation,
                        modification=modification,
                        automatic_modification=automatic,
                    )
            automatic = execute_automatic_modification(modification)
            return VerificationOutcome(
                attempt,
                HyperPayStatus.SUCCESS,
                reservation=automatic.request.reservation,
                modification=automatic.request,
                automatic_modification=automatic,
            )

        reservation = Reservation.objects.filter(booking_intent=attempt.booking_intent).first()
        if reservation is None:
            reservation = prepare_local_reservation(attempt.booking_intent)
        self._record_verified_booking_payment(attempt, reservation)
        reservation.refresh_from_db()
        if reservation.normalized_status == Reservation.Status.CONFIRMED:
            return VerificationOutcome(attempt, HyperPayStatus.SUCCESS, reservation)
        with HostawayBookingService() as booking_service:
            hostaway = booking_service.create_hostaway_reservation(reservation)
        return VerificationOutcome(attempt, HyperPayStatus.SUCCESS, reservation, hostaway)

    @staticmethod
    def _record_verified_booking_payment(
        attempt: PaymentAttempt,
        reservation: Reservation,
    ) -> None:
        """Persist the verified payment independently from Hostaway confirmation."""
        with transaction.atomic():
            locked = Reservation.objects.select_for_update().get(pk=reservation.pk)
            update_fields = ["payment_status", "updated_at"]
            locked.payment_status = "paid"
            if locked.normalized_status == Reservation.Status.AWAITING_PAYMENT:
                locked.normalized_status = Reservation.Status.READY_FOR_HOSTAWAY
                update_fields.append("normalized_status")
            locked.save(update_fields=update_fields)
            BookingIntent.objects.filter(
                pk=attempt.booking_intent_id,
                status=BookingIntent.Status.AWAITING_PAYMENT,
            ).update(
                status=BookingIntent.Status.PAYMENT_VERIFIED,
                updated_at=timezone.now(),
            )

    def _revalidate_booking_intent(self, intent: BookingIntent) -> None:
        request = AvailabilityRequest(
            property=intent.property,
            check_in=intent.check_in,
            check_out=intent.check_out,
            guests=intent.guests,
        )
        if self.availability_service is not None:
            result = self.availability_service.check(request, bypass_cache=True)
        else:
            with AvailabilityService() as service:
                result = service.check(request, bypass_cache=True)
        if not result.is_available or result.quote is None:
            BookingIntent.objects.filter(
                pk=intent.pk,
                status=BookingIntent.Status.AWAITING_PAYMENT,
            ).update(status=BookingIntent.Status.UNAVAILABLE, updated_at=timezone.now())
            raise HyperPayCheckoutError("prepayment_unavailable")
        quote = result.quote
        if (
            quote.listing_id != intent.property.hostaway_listing_id
            or quote.currency != intent.currency
            or quote.total_price != intent.total_price
        ):
            BookingIntent.objects.filter(
                pk=intent.pk,
                status=BookingIntent.Status.AWAITING_PAYMENT,
            ).update(status=BookingIntent.Status.PRICE_CHANGED, updated_at=timezone.now())
            raise HyperPayCheckoutError("prepayment_price_changed")

    def _revalidate_modification(self, modification: BookingModificationRequest) -> None:
        if self.modification_service is not None:
            result = self.modification_service.revalidate_for_payment(modification)
        else:
            with ModificationService() as service:
                result = service.revalidate_for_payment(modification)
        if result.code == "ready":
            return
        if result.code in {"price_changed", "currency_changed"}:
            status = BookingModificationRequest.Status.PRICE_CHANGED
        elif result.code in {
            "unavailable_dates",
            "inventory_conflict",
            "arrival_restricted",
            "departure_restricted",
        }:
            status = BookingModificationRequest.Status.UNAVAILABLE
        else:
            status = None
        if status:
            BookingModificationRequest.objects.filter(
                pk=modification.pk,
                status=BookingModificationRequest.Status.AWAITING_PAYMENT,
            ).update(status=status, updated_at=timezone.now())
        raise HyperPayCheckoutError(f"prepayment_{result.code}")

    @staticmethod
    def _validate_checkout_response(
        response: dict[str, Any],
        attempt: PaymentAttempt,
    ) -> tuple[str, str, str]:
        checkout_id = response.get("id")
        integrity = response.get("integrity")
        result = response.get("result")
        result_code = result.get("code") if isinstance(result, dict) else ""
        if (
            not isinstance(checkout_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{8,255}", checkout_id)
            or not isinstance(integrity, str)
            or not re.fullmatch(r"sha(?:256|384|512)-[A-Za-z0-9+/=]+", integrity)
            or map_result_code(result_code) is not HyperPayStatus.PENDING
        ):
            attempt.status = PaymentAttempt.Status.FAILED
            attempt.failure_code = "checkout_response_invalid"
            attempt.provider_result_code = (
                result_code if isinstance(result_code, str) else ""
            )
            attempt.save(
                update_fields=[
                    "status",
                    "failure_code",
                    "provider_result_code",
                    "updated_at",
                ]
            )
            raise HyperPayCheckoutError()
        attempt.provider_checkout_id = checkout_id
        attempt.provider_reference = checkout_id
        attempt.widget_integrity = integrity
        attempt.provider_result_code = result_code
        attempt.status = PaymentAttempt.Status.PENDING
        attempt.save(
            update_fields=[
                "provider_checkout_id",
                "provider_reference",
                "widget_integrity",
                "provider_result_code",
                "status",
                "updated_at",
            ]
        )
        return checkout_id, integrity, result_code

    @staticmethod
    def _log_checkout(attempt: PaymentAttempt, checkout_id: str) -> None:
        logger.info(
            "Payment checkout created: provider=hyperpay internal_payment_id=%s "
            "booking_intent_id=%s modification_request_id=%s "
            "merchant_transaction_id=%s checkout_id=%s internal_status=%s",
            attempt.pk,
            attempt.booking_intent_id,
            attempt.modification_request_id,
            attempt.merchant_transaction_id,
            checkout_id,
            attempt.status,
        )

    @staticmethod
    def _verification_mismatch(attempt: PaymentAttempt, document: dict[str, Any]) -> str:
        expected = {
            "currency": attempt.currency,
            "paymentType": settings.HYPERPAY_PAYMENT_TYPE,
            "merchantTransactionId": attempt.merchant_transaction_id,
        }
        for field, expected_value in expected.items():
            if document.get(field) != expected_value:
                return f"{field}_mismatch"
        try:
            returned_amount = Decimal(str(document.get("amount")))
        except (InvalidOperation, TypeError, ValueError):
            return "amount_invalid"
        if returned_amount != attempt.amount:
            return "amount_mismatch"
        response_entity = document.get("entityId")
        if response_entity is not None and response_entity != settings.HYPERPAY_ENTITY_ID:
            return "entity_id_mismatch"
        if document.get("paymentBrand") not in settings.HYPERPAY_ALLOWED_BRANDS:
            return "payment_brand_mismatch"
        return ""


def _safe_text(value: object, max_length: int) -> str:
    return value[:max_length] if isinstance(value, str) else ""


def _safe_optional_text(value: object, max_length: int) -> str | None:
    return value[:max_length] if isinstance(value, str) and value else None
