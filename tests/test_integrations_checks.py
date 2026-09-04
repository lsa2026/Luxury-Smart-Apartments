"""The webhook boundary must fail loudly, never silently reject Hostaway."""

import pytest
from django.test import override_settings

from apps.integrations.checks import hostaway_webhook_configuration_check

CREDENTIALS = {
    "HOSTAWAY_WEBHOOK_BASIC_AUTH_USERNAME": "synthetic-user",
    "HOSTAWAY_WEBHOOK_BASIC_AUTH_PASSWORD": "synthetic-password",
}


def ids(problems: list[object]) -> set[str]:
    return {problem.id for problem in problems}


@override_settings(HOSTAWAY_WEBHOOK_RECEIVER_ENABLED=False)
def test_a_disabled_receiver_reports_nothing() -> None:
    assert hostaway_webhook_configuration_check() == []


@override_settings(
    HOSTAWAY_WEBHOOK_RECEIVER_ENABLED=True,
    HOSTAWAY_WEBHOOK_BASIC_AUTH_USERNAME="",
    HOSTAWAY_WEBHOOK_BASIC_AUTH_PASSWORD="",
)
def test_an_enabled_receiver_without_credentials_is_an_error() -> None:
    """Otherwise every delivery 401s and bookings quietly stop updating."""
    assert "integrations.E001" in ids(hostaway_webhook_configuration_check())


@pytest.mark.parametrize(
    ("username", "password"),
    [("synthetic-user", ""), ("", "synthetic-password")],
)
@override_settings(HOSTAWAY_WEBHOOK_RECEIVER_ENABLED=True)
def test_half_configured_credentials_are_an_error(username: str, password: str) -> None:
    with override_settings(
        HOSTAWAY_WEBHOOK_BASIC_AUTH_USERNAME=username,
        HOSTAWAY_WEBHOOK_BASIC_AUTH_PASSWORD=password,
    ):
        assert "integrations.E001" in ids(hostaway_webhook_configuration_check())


@override_settings(
    HOSTAWAY_WEBHOOK_RECEIVER_ENABLED=True,
    HOSTAWAY_WEBHOOK_ALLOWED_EVENTS=("reservation.created", "reservation.updated"),
    **CREDENTIALS,
)
def test_a_fully_configured_receiver_is_clean() -> None:
    assert hostaway_webhook_configuration_check() == []


@override_settings(
    HOSTAWAY_WEBHOOK_RECEIVER_ENABLED=False,
    HOSTAWAY_WEBHOOK_PROCESSING_ENABLED=True,
)
def test_a_worker_that_only_processes_is_not_a_fault() -> None:
    """The worker and scheduler drain a queue the web service fills, and they
    serve no HTTP, so reception is legitimately off in those processes."""
    assert hostaway_webhook_configuration_check() == []


@override_settings(
    HOSTAWAY_WEBHOOK_RECEIVER_ENABLED=True,
    HOSTAWAY_WEBHOOK_ALLOWED_EVENTS=(),
    **CREDENTIALS,
)
def test_a_receiver_that_accepts_no_event_warns() -> None:
    assert "integrations.W002" in ids(hostaway_webhook_configuration_check())
