"""Provider-neutral payment boundary; real payment is intentionally disabled."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class PaymentProviderError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    booking_reference: str
    amount: Decimal
    currency: str
    return_url: str


@dataclass(frozen=True, slots=True)
class PaymentSessionResult:
    provider_reference: str
    redirect_url: str


class PaymentProvider(Protocol):
    def create_session(self, request: PaymentRequest) -> PaymentSessionResult: ...


class DisabledPaymentProvider:
    def create_session(self, request: PaymentRequest) -> PaymentSessionResult:
        raise PaymentProviderError("payment_provider_not_configured")
