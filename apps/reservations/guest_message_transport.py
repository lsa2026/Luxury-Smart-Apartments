"""Review-only transport boundary; the central YCloud wire contract is not ready.

No endpoint, credential or independent sender is introduced here. Existing domain
guards and durable claims remain with each caller. Selecting the new transport
holds before a claim; it never falls back to UltraMsg or replays old deliveries.
"""

from dataclasses import dataclass

from django.conf import settings


class TransportHeld(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def mode():
    return getattr(settings, "WEBSITE_GUEST_MESSAGE_TRANSPORT", "ultramsg")


@dataclass(frozen=True)
class Route:
    source: str
    stay_id: str
    revision: int
    purpose: str
    reference: str
    transport: str

    def open(self, legacy_factory):
        if self.transport != "ultramsg" or mode() != "ultramsg":
            raise TransportHeld("ycloud_contract_pending")
        # Defer construction until AFTER the caller's committed delivery claim,
        # preserving the original provider-configuration/error handling boundary.
        return legacy_factory()


def prepare(stay, *, source, purpose, reference, recipient_kind="guest"):
    """Called only after domain validation and before consuming a send claim.

    This is an internal seam, not a proposed central HTTP payload. Contract19,
    template/consent mapping37, source-event cutoff20 and durable transport/receipt
    correlation must all be reviewed before implementing the central branch.
    """
    selected = mode()
    if selected != "ultramsg":
        if selected != "ycloud_central":
            raise TransportHeld("unsupported_guest_transport")
        if recipient_kind != "guest":
            raise TransportHeld("workgroup_transport_not_supported")
        raise TransportHeld("ycloud_contract_pending")
    return Route(
        source, str(stay.pk), stay.communication_revision, purpose, str(reference), selected
    )


def require_legacy_receipt(attempt=None):
    # A historical, database-bound core attempt may retain its original receipt
    # reader. This cannot query/translate a YCloud result or allow a legacy send.
    if mode() != "ultramsg":
        from .legacy_message_receipts import bound_before_cutover

        if mode() != "ycloud_central" or not bound_before_cutover(attempt):
            raise ValueError("receipt_lookup_uncertain")
