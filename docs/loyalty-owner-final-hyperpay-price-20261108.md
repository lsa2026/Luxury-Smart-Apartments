# Previous-stay invitation: use the paid owner-approved final price

An owner-created reservation can preserve its original Hostaway quote while the
approved final price and successful HyperPay receipt use a lower amount. The
loyalty reader previously rejected the original quote's legacy `accommodation`
line and its mismatch with that final total, disabling both invitation channels.

For a previous-stay preview or an explicit accepted past-stay offer only, read
the booked manual draft's approved final SAR total. Require agreement with the
reservation, intent and payment amounts, matching property and stay dates, and
fully verified HyperPay receipts whose sum exactly matches the final price.
The original quote must contain a single explicit accommodation-only line;
unknown fee or discount allocation, partial/extra receipts, mismatched drafts,
other receipt providers and test-result receipts do not gain this exception.
Completed modification snapshots retain precedence.

Normalize that lodging line in memory and include the draft/quote provenance
in the financial fingerprint and eventual append-only award evidence. Keep the
original quote and all payments unchanged. The existing independent enrollment,
phone verification, shared invitation deduplication, once-only posting and
current/future-stay restrictions remain in force. No membership, invitation,
points or guest messages are created by a read-only preview. No data migration,
scheduler, n8n, credential or cancellation changes are included.

Synthetic regression: original quote SAR 700, approved and HyperPay-paid total
SAR 600 produces a SAR 600 preview and 18 points after the explicit invitation
and independent enrollment/phone verification, exactly once, on both channels.
