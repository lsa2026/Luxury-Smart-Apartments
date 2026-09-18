# Owner modification incident — 18 September 2026

## Verified cause

Production failed in `ModificationService.set_owner_final_total` with PostgreSQL
`FOR UPDATE cannot be applied to the nullable side of an outer join`.
The query joined the nullable reservation booking intent while attempting to lock
every joined table. SQLite tests silently omitted row locks and missed the defect.
The same unsafe query existed in modification checkout and refund preparation.

## Repair

- Lock the intended root row explicitly; serialize owner changes by reservation.
- Test the original failing SQL as a PostgreSQL-only regression control, then test
  real owner POSTs using actual database transactions (external providers stubbed).
- Preserve the administrator's final total and align Hostaway finance components
  with that total, using the same override builder as new manual bookings.
- Compute outstanding/refundable amounts from verified net payments, not the
  difference between two quoted totals. Unpaid reservations never generate refunds.
- Confirm a due-payment owner's modification in Hostaway before asking Aseel for
  the outstanding amount. Redirect to the booking list after accepted delivery.
- Keep the Hostaway operation and WhatsApp delivery ledgers: repeat submissions do
  not repeat the external update or send another accepted message.
- Refuse stale, expired, superseded, unapproved or changed-baseline actions; a new
  owner search gets a fresh proposal, and already-modified bookings are editable.
- Only claim a refund was submitted when HyperPay confirms acceptance; validate
  unsupported amounts before recording any submission. Permit a later partial
  refund against the remaining balance of the original payment.
- Do not silently treat cross-currency verified payments as zero or create a new
  settlement while an earlier refund remains unresolved. These need reconciliation.
- Add a PostgreSQL 17 CI job for the booking/payment regression suites.

## Verification and boundaries

Final local results: **136 passed on PostgreSQL 17** for the focused suites;
**1315 passed, 10 pre-existing failures, 1 PostgreSQL-only skip** for the full
SQLite suite. Changed Python files pass Ruff, Django system checks pass and
`makemigrations --check` reports no missing migrations.

Focused PostgreSQL suites cover price approval, unpaid lower/equal/higher final
totals, partial/full payments, refund preparation, repeat submission, repeated
modification, superseded proposals, changed payment balance, Hostaway timeout and
WhatsApp failure. No actual customer booking, refund or accounting message is
created by these tests.

The full SQLite suite was also run. Ten pre-existing failures were reproduced
against clean commit `7402c33`: old admin language/navigation/draft UI assertions,
missing `modification_refund_admin_alert` copy in three languages, and three
refund-policy tests whose fixtures have no successful payment. They are not proof
that this whole repository is green; they are separate existing follow-up work.

This repair does not turn on unavailable HyperPay manual-link functionality,
replace the Hostaway synchronization architecture, or implement splitting one
refund across multiple captures. An unsupported/refused/ambiguous provider
operation must remain visible for reconciliation, never reported as success or
blindly retried. Production smoke checks are read-only; provider calls are tested
with controlled responses rather than sending live financial transactions.
