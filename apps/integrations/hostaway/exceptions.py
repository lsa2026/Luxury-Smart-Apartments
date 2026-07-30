"""Domain exceptions raised by the Hostaway integration."""


class HostawayError(Exception):
    """Base exception for expected Hostaway failures."""


class HostawayConfigurationError(HostawayError):
    """The local Hostaway configuration is incomplete or unsafe."""


class HostawayNetworkError(HostawayError):
    """A network failure prevented a Hostaway response."""


class HostawayTimeoutError(HostawayNetworkError):
    """Hostaway did not respond within the configured timeout."""


class HostawayAuthenticationError(HostawayError):
    """Hostaway rejected the configured credentials."""


class HostawayNotFoundError(HostawayError):
    """The requested Hostaway resource does not exist."""


class HostawayRateLimitError(HostawayError):
    """Hostaway continued to rate-limit requests after bounded retries."""


class HostawayServerError(HostawayError):
    """Hostaway continued to fail after bounded retries."""


class HostawayResponseError(HostawayError):
    """Hostaway returned a malformed or unexpected response."""


class HostawayAvailabilityError(HostawayError):
    """Hostaway rejected dates, stay restrictions, or price calculation."""


class HostawaySyncAlreadyRunningError(HostawayError):
    """Another Hostaway sync of the same type is already running."""
