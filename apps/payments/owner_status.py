"""Private, database-only change detection for visible administration bookings."""

import hashlib
import json
from uuid import UUID

from apps.reservations.models import Reservation

from .models import ReservationPaymentRequest


def booking_status_fingerprint(ids):
    if len(ids) > 100:
        raise ValueError("too_many_bookings")
    ids = [UUID(str(value)) for value in ids]
    bookings = list(
        Reservation.objects.filter(pk__in=ids)
        .order_by("pk")
        .values_list("pk", "payment_status", "normalized_status", "updated_at")
    )
    links = list(
        ReservationPaymentRequest.objects.filter(reservation_id__in=ids)
        .order_by("pk")
        .values_list("pk", "status", "paid_at")
    )
    return hashlib.sha256(json.dumps([bookings, links], default=str).encode()).hexdigest()
