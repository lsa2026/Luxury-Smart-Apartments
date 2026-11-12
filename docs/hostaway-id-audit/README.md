# HOSTAWAY-ID-20261123 - payment identity compatibility

Reviewed 12 November 2026. Source Gmail message: 1a1cf14f815fb1d4
(business account, Hostaway, 12:27 Riyadh).
This case was not found in the conversation, GitHub PR search or Linear search
before implementation. LUX-52 and LUX-18 are historical context, not proof of
an incident or a new authorization to send invoices or guest messages.

Official guide:
https://support.hostaway.com/hc/en-us/articles/58226558541083-Hostaway-Public-API-IDs-can-be-returned-as-text-from-November-23-2026

The announced rollout begins on 23 November with payment cards, guest charges
and guest invoice charges. Both integer and string forms can appear. Other
endpoints follow later, with no date stated in this notice. No current failed
payment, booking or delivery is inferred from the announcement.

## Confirmed initial-rollout dependency and fix

The custom Django client uses GET /guestPayments/charges and POST
/guestPayments/charges/{reservationId} for already-verified, external HyperPay
receipt bookkeeping. It does not collect money through that endpoint.

Before this change:
- POST response validation rejected a string charge ID.
- GET and POST compared reservationId using integer-sensitive equality.
- HostawayFinancialEntry.remote_charge_id was a PositiveBigIntegerField.

The new payment boundary accepts integer or string charge identities and returns
text without float conversion. Exact reservation binding remains mandatory.
The receipt ledger validates and stores text; migration 0021 changes only the
remote charge identity column to VARCHAR(64), preserving old values and NULL.
Amounts, currencies, payment verification, receipt markers and uncertain-outcome
retry protection are unchanged. Malformed IDs fail closed.

Files: apps/integrations/hostaway/identifiers.py, client.py,
apps/payments/hostaway_ledger.py, models.py and migration 0021.

The repository and the eight active Apartment Pathway definitions contain no
Hostaway payment-card or guest-invoice-charge calls. Local guest invoices
(LUX-52) use reservation reads and the website PDF/delivery system; they are not
Hostaway /guestInvoices/{id}/charges calls. Absence is limited to the reviewed
custom source and exposed workflows, not all Marketplace partner internals.

## Automation evidence and isolation

All eight active Apartment Pathway definitions (01-08) were read. Versions that
had an unpublished draft were not published or overwritten. The active06
version e25d69e2-03ed-4216-a026-23bb35ceffae was separately retrieved.

Pathway03 sNwA2n4FfnAMqFDW active version
9238ff39-db57-4497-b307-69be839298a9 uses text reservation keys in its form,
Hostaway URL construction and read-back comparison. Node103D is captured in
pathway03-phone-verification.js without credentials or connections.
The Node VM test runs only that pure node with synthetic input: legacy number,
string, long string, wrong ID and repeated response. It sends no HTTP request,
does not execute a production graph and does not establish delivery of messages.
No production n8n workflow was modified for this payment-only rollout.

## Remaining later-endpoint work - not certified complete

These are known future compatibility dependencies, not current incidents:
- Website reservation search/history pages still require numeric reservation
  identities; core reservation/listing/review DTOs and several model columns
  retain integer IDs. A coordinated end-to-end text migration must include
  routes, forms, database fields, comparisons, cursors, caches, JSON/event
  contracts and JavaScript before those endpoints change or letters appear.
- In Pathway01 and Pathway06 normalization, reservation_key preserves the long
  string but the auxiliary id uses numberOrNull. A synthetic value
  "957144035275968076" becomes 957144035275968100 in that auxiliary field.
  listing_map_id is also numeric. The current notice does not announce those
  reservation/listing endpoint dates. Their Supabase schemas and all consumers
  must be coordinated before converting the production fields.
- Pathway03 form currently allows digit-only keys up to30 digits, so a future
  alphabetic reservation ID would require a separate form/contract change.
- A legacy JSON numeric ID outside JavaScript's safe integer range is already
  rounded by ordinary JSON parsing; String() cannot recover it. Such input must
  be rejected or parsed losslessly when upgrading the relevant HTTP boundaries.

Do not declare universal Hostaway ID readiness from this scoped payment fix.
Do not blindly rerun any production receipt in unknown/review/posting state.
Before a future deployment that changes reservation/listing IDs, agree and test
the database/event migration together with the website and n8n owners.

## Reproducible validation and deployment limits

- pytest tests/test_hostaway_payment_identifiers.py tests/test_sync_policy_october.py
- node --test tests/hostaway-payment-id-automation.test.cjs
- Ruff lint/format, full Quality gate and PostgreSQL job.

Tests use httpx.MockTransport or fake clients and a test database. The migration
test checks old numeric/NULL records, a long ID, a value exceeding64-bit storage,
and text with letters. Reconciliation tests recover an unknown receipt by its
existing marker without reposting, and repeat execution without duplicate writes.

No live charge, invoice issue/send, booking create/cancel, guest contact,
permission or secret change is part of this case. Production verification is
deployment/migration logs and public health, not a live financial transaction.
After text IDs exist, do not reverse the database field to integer automatically;
retain the expanded text schema during an application rollback.
