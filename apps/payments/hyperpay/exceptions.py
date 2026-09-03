"""Safe exceptions which never contain credentials or raw provider payloads."""


class HyperPayError(Exception):
    code = "hyperpay_error"

    def __init__(self, code: str | None = None) -> None:
        if code:
            self.code = code
        super().__init__(self.code)


class HyperPayConfigurationError(HyperPayError):
    code = "hyperpay_not_configured"


class HyperPayConnectionError(HyperPayError):
    code = "hyperpay_unavailable"


class HyperPayAuthenticationError(HyperPayError):
    code = "hyperpay_authentication_failed"


class HyperPayResponseError(HyperPayError):
    code = "hyperpay_invalid_response"


class HyperPayCheckoutError(HyperPayError):
    code = "hyperpay_checkout_failed"


class HyperPayVerificationError(HyperPayError):
    code = "hyperpay_verification_failed"
