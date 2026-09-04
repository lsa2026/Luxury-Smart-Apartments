"""Django system checks for the Hostaway webhook boundary."""

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security)
def hostaway_webhook_configuration_check(
    app_configs: object = None,
    **kwargs: object,
) -> list[Error | Warning]:
    del app_configs, kwargs
    problems: list[Error | Warning] = []
    receiver_enabled = settings.HOSTAWAY_WEBHOOK_RECEIVER_ENABLED
    credentials_set = bool(
        settings.HOSTAWAY_WEBHOOK_BASIC_AUTH_USERNAME
        and settings.HOSTAWAY_WEBHOOK_BASIC_AUTH_PASSWORD
    )
    if receiver_enabled and not credentials_set:
        # Without them the endpoint answers 401 to Hostaway forever, and nothing
        # in the product reports it: bookings simply stop updating.
        problems.append(
            Error(
                "The Hostaway webhook receiver is enabled without basic-auth "
                "credentials, so every delivery would be rejected.",
                hint=(
                    "Set HOSTAWAY_WEBHOOK_BASIC_AUTH_USERNAME and "
                    "HOSTAWAY_WEBHOOK_BASIC_AUTH_PASSWORD, and use the same pair "
                    "in the Hostaway webhook's Login and Password fields."
                ),
                id="integrations.E001",
            )
        )
    # Processing without reception is not a fault: the worker and scheduler drain
    # a queue the web service fills, and they serve no HTTP of their own.
    if receiver_enabled and not settings.HOSTAWAY_WEBHOOK_ALLOWED_EVENTS:
        problems.append(
            Warning(
                "The Hostaway webhook receiver accepts no event type, so every "
                "delivery is stored as ignored.",
                hint="Set HOSTAWAY_WEBHOOK_ALLOWED_EVENTS to the events to act on.",
                id="integrations.W002",
            )
        )
    return problems
