"""Synthetic HTTP/DB coverage for HOSTAWAY-ID-20261123; no provider writes."""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayResponseError
from apps.integrations.hostaway.identifiers import payment_identifier

LARGE = "957144035275968076"


@pytest.mark.parametrize("identifier", [123, "123", LARGE, int(LARGE), "HA-charge_2026-A"])
def test_payment_identifiers_keep_exact_characters(identifier):
    assert payment_identifier(identifier) == str(identifier)


@pytest.mark.parametrize(
    "identifier",
    [
        None,
        True,
        False,
        0,
        -1,
        "0",
        "",
        " 123",
        "123 ",
        1.0,
        float(LARGE),
        [],
        {},
        "a" * 65,
        "../123",
        "123?other=1",
        "\u0661\u0662\u0663",
    ],
)
def test_ambiguous_or_invalid_payment_identity_is_rejected(identifier):
    with pytest.raises(HostawayResponseError):
        payment_identifier(identifier)


@pytest.mark.parametrize("identifier", [123, "123", LARGE, int(LARGE), "HA-charge_2026-A"])
@pytest.mark.parametrize("reservation", [900111, "900111"])
def test_charge_get_and_post_accept_both_id_types_without_rounding(
    settings, identifier, reservation
):
    settings.HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED = True
    charge = {
        "id": identifier,
        "reservationId": reservation,
        "status": "paid",
        "currency": "SAR",
        "title": "Synthetic receipt",
        "amount": "123.45",
        "paymentProvider": "offline",
    }
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "result": [charge] if request.method == "GET" else charge,
            },
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.hostaway.com/v1"
    ) as http:
        client = HostawayClient(client=http, access_token="synthetic")
        read = client.get_guest_charges(900111)
        created = client.create_offline_paid_charge(
            900111,
            title=charge["title"],
            amount=Decimal("123.45"),
            currency="SAR",
            description="Synthetic only",
            paid_at=datetime(2026, 11, 12, tzinfo=UTC),
        )
    assert read == [created]
    assert created["id"] == str(identifier)
    assert created["reservationId"] == "900111"
    assert created["amount"] == "123.45"
    assert charge["id"] == identifier  # Do not mutate the raw response.
    assert [request.method for request in calls] == ["GET", "POST"]
    assert calls[0].url.params["reservationId"] == "900111"
    assert calls[1].url.path == "/v1/guestPayments/charges/900111"


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("invalid", [True, None, 1.0, "", "a" * 65])
def test_invalid_charge_id_fails_closed(settings, method, invalid):
    settings.HOSTAWAY_FINANCIAL_RECEIPTS_ENABLED = True
    charge = {"id": invalid, "reservationId": "900111"}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "status": "success",
                "result": [charge] if request.method == "GET" else charge,
            },
        )
    )
    with httpx.Client(transport=transport, base_url="https://api.hostaway.com/v1") as http:
        client = HostawayClient(client=http, access_token="synthetic")
        with pytest.raises(HostawayResponseError):
            if method == "get":
                client.get_guest_charges(900111)
            else:
                client.create_offline_paid_charge(
                    900111,
                    title="Synthetic",
                    amount=Decimal("1"),
                    currency="SAR",
                    description="Synthetic",
                    paid_at=datetime(2026, 11, 12, tzinfo=UTC),
                )


@pytest.mark.parametrize("reservation", ["900112", True, 900111.0, "900111 "])
def test_text_conversion_does_not_weaken_reservation_binding(reservation):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "status": "success",
                "result": [{"id": LARGE, "reservationId": reservation}],
            },
        )
    )
    with httpx.Client(transport=transport, base_url="https://api.hostaway.com/v1") as http:
        with pytest.raises(HostawayResponseError):
            HostawayClient(client=http, access_token="synthetic").get_guest_charges(900111)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("legacy_value", [None, 957144035275968076])
def test_charge_storage_migration_preserves_legacy_value_and_null(legacy_value):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from apps.payments.models import HostawayFinancialEntry
    from tests.test_sync_policy_october import paid_reservation

    _, payment = paid_reservation()
    executor = MigrationExecutor(connection)
    before = [("payments", "0020_dayusepaymentattempt_stay_and_more")]
    after = [("payments", "0021_hostaway_charge_id_as_text")]
    executor.migrate(before)
    try:
        old = executor.loader.project_state(before).apps.get_model(
            "payments", "HostawayFinancialEntry"
        )
        entry = old.objects.create(payment_attempt_id=payment.pk, remote_charge_id=legacy_value)
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        entry = HostawayFinancialEntry.objects.get(pk=entry.pk)
        assert entry.remote_charge_id == (str(legacy_value) if legacy_value is not None else None)
        entry.remote_charge_id = "charge_" + "9" * 30
        entry.save(update_fields=["remote_charge_id"])
        entry.refresh_from_db()
        assert entry.remote_charge_id == "charge_" + "9" * 30
        entry.remote_charge_id = None
        entry.save(update_fields=["remote_charge_id"])
        entry.refresh_from_db()
        assert entry.remote_charge_id is None
    finally:
        MigrationExecutor(connection).migrate(after)
