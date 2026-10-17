# Signed loyalty correction balances

Owner decision, 17 October 2026: show a points account below zero after reversing
rewards that have already been spent or gifted. This supersedes deferred recovery.
It does not authorise cancelling the owner's booking for a test.

## Accounting

- A full eligible reward correction is one new debit linked to the original earn
  entry. No history, rates, gifts or recipient balances are rewritten.
- Example: SAR 957 earns 28.71, gifting 3 leaves 25.71; full reversal debits 28.71
  and leaves the sender at -3.00. The recipient keeps 3.00.
- All subsequent credits (earned rewards, voluntary incoming gifts and restored
  redemption points) reduce the signed shortfall naturally through the ledger.
  The system never withdraws money from a bank card to cover a negative balance.
- Available = max(0, ledger balance - actual outstanding holds). Actual holds
  are calculated independently, never inferred as balance minus available.
- New gifts and new holds still require positive available funds. Existing
  authorised holds may be redeemed even after a reward correction, avoiding a
  failed settlement on a separately confirmed booking. The same hold is consumed
  once. This is not a new overdraft or permission to alter a hold from a browser.
- Closing membership forfeits only positive points; it neither creates a negative
  debit nor silently forgives an existing negative balance.

## Migration, integrity and backups

Migration 0005 removes the old nonnegative check, retaining one-sided entries,
uniqueness, append-only history and running-balance/transfer guards. Negative
account rows require a credit, linked adjustment or redemption. A programme
running total may be negative too (e.g. a gift recipient forfeited points before
the sender's original award was reversed); it remains the exact signed sum.

Legacy deferred corrections become linked ledger debits once, using the correct
database alias, inside the migration transaction and financial lock. No recipient
rows change. This accounting migration does not dispatch historical notices.
Invalid legacy references fail closed. Populated ledger downgrade is blocked;
an empty rehearsal database remains reversible. SQLite history triggers are
reinstalled after constraint table rebuilds. Backup validation and restoration
accept valid signed balances and continue rejecting arithmetic discrepancies.

## Published explanation

Issue terms edition v4 at a new URL in Arabic, English and French. Earlier issued
content, hashes and member consent history remain intact. The already-approved
v3 configuration pair selects v4 at runtime for new enrolments and the current
terms link, without changing stored environment variables, permissions or launch
gates. Disabled/custom/preview configurations are not promoted. The v4 text only
updates the correction paragraph and release label; legal exit/expiry/cash-out
proposals remain unadopted. This documents the owner's product decision, not a
legal opinion or a claim that earlier consent was renewed.

## Verification scope

Use fictional offline tests for the 957 example, subsequent rewards and gifts,
unchanged recipient balance, duplicate reconciliation, spending rejection,
honouring a prior hold, legacy conversion idempotency, signed backup round-trip,
membership closure, UI explanation and three-language versioned consent.
Run PostgreSQL CI to verify the production-engine migration and history guards.
Verify live schema metadata, aggregate balances and the new public document after
deployment. Do not cancel, refund, transfer or send a real message for validation.

An intermittent local restore failure was reproduced during verification: Django
serialized a sub-millisecond timestamp as `...48.000Z` and reloaded it as
`...48Z`. Canonicalization now removes only the redundant `.000` from declared
datetime fields when capturing/comparing records. Legacy snapshot payloads and
checksums remain untouched; JSON evidence and arbitrary strings are not
normalized. Tests cover legacy member/ledger/user timestamps and continue to
reject an actual one-second change in financial history.
