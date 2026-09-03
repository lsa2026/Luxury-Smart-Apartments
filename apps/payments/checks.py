"""Django system checks for explicit HyperPay and Hostaway automation boundaries."""

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security)
def hyperpay_configuration_check(
    app_configs: object = None,
    **kwargs: object,
) -> list[Error]:
    del app_configs, kwargs
    if not settings.HYPERPAY_ENABLED:
        return []
    errors: list[Error] = []
    approved_url = settings.HYPERPAY_APPROVED_BASE_URLS.get(settings.HYPERPAY_ENVIRONMENT)
    if approved_url is None:
        errors.append(Error("HyperPay environment is invalid.", id="payments.E101"))
    if approved_url is not None and settings.HYPERPAY_BASE_URL != approved_url:
        errors.append(
            Error(
                "HyperPay base URL does not match its environment.",
                id="payments.E102",
            )
        )
    if settings.HYPERPAY_CURRENCY != "SAR" or settings.HYPERPAY_PAYMENT_TYPE != "DB":
        errors.append(Error("HyperPay requires SAR/DB in this phase.", id="payments.E103"))
    if not settings.HYPERPAY_ENTITY_ID or not settings.HYPERPAY_ACCESS_TOKEN:
        errors.append(Error("HyperPay credentials are missing.", id="payments.E104"))
    if (
        settings.HYPERPAY_ENVIRONMENT == "test"
        and (
            settings.HOSTAWAY_LIVE_BOOKING_ENABLED
            or settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED
            or settings.HOSTAWAY_LIVE_EXTENSION_ENABLED
            or settings.HOSTAWAY_LIVE_CANCELLATION_ENABLED
        )
    ):
        errors.append(
            Error(
                "HyperPay TEST cannot be combined with live Hostaway writes.",
                id="payments.E105",
            )
        )
    if (
        settings.HYPERPAY_ENVIRONMENT == "production"
        and not settings.HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED
    ):
        errors.append(
            Error(
                "Production payment requires live pre-payment availability revalidation.",
                id="payments.E106",
            )
        )
    if (
        settings.HYPERPAY_ENVIRONMENT == "production"
        and not settings.HOSTAWAY_LIVE_BOOKING_ENABLED
    ):
        errors.append(
            Error(
                "Production payment requires automatic live Hostaway booking writes.",
                id="payments.E107",
            )
        )
    if settings.BOOKING_AUTOMATIC_MODIFICATION_APPROVAL and not (
        settings.HOSTAWAY_LIVE_MODIFICATION_ENABLED
        and settings.HOSTAWAY_LIVE_EXTENSION_ENABLED
    ):
        errors.append(
            Error(
                "Automatic modifications require live Hostaway modification and extension.",
                id="payments.E108",
            )
        )
    if settings.BOOKING_AUTOMATIC_CANCELLATION_ENABLED and not (
        settings.BOOKING_AUTOMATIC_MODIFICATION_APPROVAL
        and settings.HOSTAWAY_LIVE_CANCELLATION_ENABLED
    ):
        errors.append(
            Error(
                "Automatic cancellation requires automatic approval and live cancellation.",
                id="payments.E109",
            )
        )
    return errors
