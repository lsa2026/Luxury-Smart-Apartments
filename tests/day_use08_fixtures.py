"""Factories for the isolated line-protocol integration test, never production."""

import hashlib
from unittest.mock import patch

import pytest
from django.conf import settings

from apps.reservations.day_use_hostaway import record_in_hostaway
from apps.reservations.day_use_stays import create_stay
from apps.reservations.models import DayUse08DeliveryAttempt
from tests.test_day_use08 import request, snapshot
from tests.test_day_use08 import rig as fixture_rig


def confirmed():
    with pytest.MonkeyPatch.context() as monkeypatch:
        r = fixture_rig.__wrapped__(settings, monkeypatch)
        stay = create_stay(**r.values)
        settings.DAY_USE08_ENABLED = True
        r.remote.create_reservation_with_price_details.return_value.snapshot = snapshot(r)
        r.remote.get_reservation.return_value = snapshot(r)
        with (
            patch("apps.reservations.day_use_ownership.sync", return_value=True),
            patch("apps.reservations.day_use_ownership.queue"),
        ):
            record_in_hostaway(request=request(r), stay_id=stay.pk, client=r.remote)
        stay.refresh_from_db()
        return stay, r.remote


def receipts(stay):
    phone = stay.guest_phone.lstrip("+")
    stamp = int(stay.starts_at.timestamp() * 1000)
    suffixes = [
        f"r{stay.communication_revision}:status",
        "rules:" + phone,
        f"guide:{stay.property.hostaway_listing_map_id}:{stamp}:{phone}",
    ]
    for suffix in suffixes:
        reference = (
            "lsa08-"
            + hashlib.md5(f"lsa08:{stay.pk}:{suffix}".encode(), usedforsecurity=False).hexdigest()
        )
        DayUse08DeliveryAttempt.objects.get_or_create(
            reference_id=reference,
            defaults={
                "stay": stay,
                "revision": stay.communication_revision,
                "payload_hash": "synthetic",
                "state": "accepted",
                "provider_id": "synthetic-" + reference,
            },
        )
