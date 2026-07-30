"""Safe configuration checks for outbound email."""

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security)
def email_configuration_check(
    app_configs: object = None,
    **kwargs: object,
) -> list[Error]:
    del app_configs, kwargs
    errors: list[Error] = []
    if settings.EMAIL_USE_TLS and settings.EMAIL_USE_SSL:
        errors.append(
            Error(
                "EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be enabled.",
                id="notifications.E001",
            )
        )
    if settings.EMAIL_DELIVERY_ENABLED and not settings.DEFAULT_FROM_EMAIL:
        errors.append(
            Error(
                "DEFAULT_FROM_EMAIL is required when email delivery is enabled.",
                id="notifications.E002",
            )
        )
    if (
        settings.EMAIL_DELIVERY_ENABLED
        and settings.EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend"
        and not settings.EMAIL_HOST
    ):
        errors.append(
            Error(
                "EMAIL_HOST is required for the SMTP backend.",
                id="notifications.E003",
            )
        )
    return errors
