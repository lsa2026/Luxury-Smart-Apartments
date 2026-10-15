"""Shared atomic reference uniqueness for legacy and operations cash receipts."""

import hashlib
import unicodedata

from django.core.exceptions import ValidationError

from .models import ManualReceiptReference


def reference_digest(reference):
    normalized = unicodedata.normalize("NFKC", reference.strip()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def claim_reference(reservation, reference):
    # Caller owns a transaction and the reservation lock. The PK serializes
    # same-reference claims even across different bookings and entry points.
    claim, _ = ManualReceiptReference.objects.get_or_create(
        digest=reference_digest(reference), defaults={"reservation": reservation}
    )
    if claim.reservation_id != reservation.pk:
        raise ValidationError("مرجع القبض مسجل لحجز آخر؛ لا يمكن تكراره.")
    return claim
