"""Project one already-verified central receipt onto its original message resource.

Never creates a checkout, collection, refund, reservation or message attempt.
Historical expectation import remains independent of future-send enable flags.
"""

from django.db import transaction
from django.utils import timezone

from apps.properties.models import Property

from . import message_source_authorization as source
from .website_financial_source_v2 import modules

RANK = {"waiting_core": 0, "queued": 0, "accepted": 1, "delivered": 2, "read": 3}


def project_payment_link_receipt(expectation):
    row = expectation.relay
    if (
        row.message_type != "payment_link"
        or row.financial
        or row.scenario not in source.SOURCES
        or expectation.attempts_claimed != 1
        or expectation.state not in {"accepted", "delivered", "read"}
        or not expectation.provider_message_id
        or len(expectation.provider_message_id) > 255
        or expectation.identity.get("source_family") != f"lsa{row.scenario}_payment_link"
        or expectation.identity.get("source_event_key")
        != f"website:{row.scenario}:payment_link:{row.event_id}"
    ):
        return False
    stays = source.SOURCES[row.scenario][0]
    links, _ = modules(row.scenario)
    pointer = stays.objects.filter(pk=row.stay_id).values_list("property_id", flat=True).first()
    if pointer is None:
        return False
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=pointer)
        stay = stays.objects.select_for_update().get(pk=row.stay_id)
        delivery = stay.payment_link_deliveries.select_for_update().filter(pk=row.event_id).first()
        try:
            phone = source.normalize_phone_number(stay.guest_phone)
        except source.InvalidPhoneNumber:
            return False
        recipient = (
            "site01:"
            + source.salted_hmac(
                "website-source-recipient.v1",
                f"{row.scenario}:{stay.pk}:{phone}",
                algorithm="sha256",
            ).hexdigest()
        )
        if (
            delivery is None
            or stay.property_id != pointer
            or delivery.fingerprint != links.fingerprint(stay)
            or str(stay.communication_revision) != row.revision
            or expectation.identity.get("reservation_revision") != row.revision
            or expectation.identity.get("recipient_ref") != recipient
            or delivery.state not in RANK
            or delivery.provider_id not in {"", expectation.provider_message_id}
        ):
            return False  # A historical receipt never releases a changed booking.
        if RANK[expectation.state] >= RANK[delivery.state]:
            delivery.state = expectation.state
            delivery.provider_id = expectation.provider_message_id
            if delivery.completed_at is None:
                delivery.completed_at = timezone.now()
            delivery.save(update_fields=["state", "provider_id", "completed_at"])
        return True
