# Payment link review corrections — 2026-10-15

## Observed production failure

The owner confirmed WhatsApp receipt, but the two HTTPS form submissions at
13:20:01 and 13:21:16 UTC failed Django CSRF origin checking: `Origin: null`.
No gateway checkout was reached by these rejected requests. The payment-link
form imposed `Referrer-Policy: no-referrer`, unlike the normal checkout's
`same-origin` policy. Do not exempt CSRF, trust `null`, or broaden trusted origins.

Form-bearing responses now use `same-origin`; result-only pages retain
`no-referrer`. Cross-origin destinations receive no referrer. Tests enforce real
CSRF cookies/tokens and verify accepted same-origin Origin/Referer paths and
continued rejection of null/untrusted origins. A mobile Safari retest on the
actual link is still required; local Chromium is not a Safari substitute.

## Shared address flow

Normal checkout and reservation-payment links include the same billing-fields
partial and use GuestDetailsForm validation, country localization, and the
existing optional address-search JavaScript, CSS, component mapping and limits.
The original quote authorization is unchanged. A separate signed payment-link
adapter checks the open request and binds short-lived address tickets to the
guest's own browser marker; it grants no ownership of the original quote or
booking management. No external browser SDK, CSP expansion, new provider key,
paid plan or infrastructure resource was added. Existing Places activation and
shared allowances apply; manual entry remains available on provider failure.
The capability and reservation data are not forwarded to Google. No address
search creates a payment attempt or writes a reservation.
The website project's read-only review found a language-renewal edge case:
address endpoints now retain the payment request's language throughout ticket
selection and expiry renewal, even when the browser requests another language.
The regression also uses a consumed, expired original quote and verifies that
the separate payment-link authorization remains valid without quote ownership.

## Message identity

The design project's guidance was read: quiet, clear and concise Luxury Living
branding. A single payment_message renderer now serves queued guest messages and
explicit owner-review copies, with localized stay dates, total, separated CTA
and safe-payment explanation. Owner-review text is opt-in, never included in
ordinary guest delivery. Existing messages are not edited or automatically resent.

## Attested external receipts

The external reference is optional. An omitted reference generates a stable
per-reservation `internal-receipt:` identifier, explicitly not external bank or
gateway evidence. The reservation lock, full exact amount/currency check,
confirmation, immutable audit, duplicate-reference registry and unresolved-payment
guards remain. Repeated blank-reference submissions are idempotent. User input
cannot impersonate the reserved internal-reference namespace.

`external_link` is the administration method for a payment ALREADY collected via
an external link prepared by accounting. It remains a manual attestation, not a
verified HyperPay transaction, and never triggers an automatic card refund.
Migration 0013 only extends method choices; it does not rewrite receipts,
references or guest data. Backup schema hashing does not include choices, so the
concrete schema signature remains compatible. No live receipt was recorded to
test the interface. No charge, refund, booking cancellation or guest contact
change is part of this repair.

## Release checks

Run focused payment-link, receipt, CSRF-log, address service and billing form
tests; shared address/draft JavaScript tests; full quality and PostgreSQL CI
(including concurrency); migration drift and lint checks. Check Arabic, English
and French at 320/390/430px, manual fallback and desktop layout. After publication
verify the actual production headers, optional reference and method choices
without charging or falsely attesting receipt. Ask the owner to reopen the
existing WhatsApp link for a fresh form before the Safari retest.
