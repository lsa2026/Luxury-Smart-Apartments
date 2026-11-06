# MorningStay customer validation — 2026-11-07

Scope: independent morning payment and its operations billing forms only.
Ordinary reservations, payment links, shared guest forms, the provider client and
the card widget are unchanged. No migration, secret or environment change.

## Sources and exact interpretation

Reviewed 2026-11-07:

- HyperPay integration entrypoint: https://www.hyperpay.com/integration-guide/
  identifies COPYandPAY as the browser-to-provider card-data integration.
- That page's tenant documentation link,
  https://hyperpay.docs.oppwa.com/integrations/widget, and the tenant parameter
  reference returned HTTP 404 during this review. Do not claim the tenant-specific
  documentation or acquiring-bank overrides were verified.
- Current official Open Payment Platform reference:
  https://docs.oppwa.com/reference/parameters (Customer, Billing Address).
  This is the same OPPWA platform used by the production integration; the live
  service configuration was read without revealing credentials: enabled,
  production, `https://eu-prod.oppwa.com/`, SAR, DB.
- https://docs.oppwa.com/integrations/widget describes preparation, hosted card
  form and server-side status retrieval. We preserve this architecture and its
  existing integrity, verification, reconciliation and duplicate-charge guards.
- https://html.spec.whatwg.org/multipage/input.html#email-state-(type=email)
  supplies the email syntax expression, used identically in both runtimes.
  The provider documents email length and meaning, not a complete syntax regex.

| Submitted parameter | Applied contract |
| --- | --- |
| customer.givenName, customer.surname | Each nonblank, maximum 50 characters; Unicode allowed. Required because this checkout sends customer parameters. |
| customer.email | 6–128 characters and HTML email syntax. Kept required by this merchant's existing checkout; the API marks it optional/conditional for risk checks. |
| billing.street1 | 1–100 characters, Unicode allowed; not only digits (the documented street combination restriction; this checkout sends no street2). |
| billing.city | 1–80 characters, Unicode allowed; required for 3DS2. |
| billing.state | 1–50 ASCII letters, digits or dots. Kept required by this merchant's existing checkout; the API marks it conditional. |
| billing.postcode | 1–16 ASCII letters/digits; required for 3DS2. No silent space/hyphen removal in this morning form. |
| billing.country | Actual ISO 3166-1 alpha-2 choice, uppercase; required for 3DS2. |

Payment amount, currency, transaction identifier and payment type remain generated
by the existing server flow. MorningStay business limits (availability, occupancy,
time window, phone normalization and approved price) are not presented as HyperPay
requirements. Phone is shown for booking reference; it is not sent in this checkout
payload, so no invented gateway phone restriction is added.

The same field expressions are emitted by the server to morning-only widgets and
executed by the browser. ASCII edge whitespace is trimmed consistently. Embedded
NUL is rejected as a local form safety rule. No transliteration, markup stripping,
truncation or invented first/surname values. Names and email can be corrected for
the payer without changing the booking identity. Valid manager-saved billing
remains review-only; invalid saved billing explicitly names the fields needing
management correction and blocks payment. A previously valid address that changes
during payment is checked again under the database lock.

Arabic is the default; English guest links follow `MorningStay.guest_language`.
Persistent bold help, per-field live errors, consent checks and the server gate
prevent payment creation from invalid customer data. Disabling JavaScript does
not bypass the server gate. Search suggestions can still be selected, but values
outside the payment contract must be corrected; suggestions do not certify payment
validity.

## Verification and release

Synthetic tests cover documented boundaries, Arabic/punctuation, Unicode character
count, invalid email/country/address, retained bound errors, both page languages,
stale legal acceptance, saved billing, direct service bypass and the production
payload with a fake provider. The actual browser validator is executed against
the same boundary matrix. Existing morning, ordinary payment and provider tests
provide regression coverage. CI also runs PostgreSQL booking regressions and the
existing responsive morning layout check.

Release through a branch and pull request; merge only after the quality gate
passes. Production uses the existing main-branch auto deploy. Verify the deployed
commit, terminal live status and public health/static assets. Do not create a
production booking, charge a card or send a guest message as a smoke test.

Rollback: revert this isolated commit and allow the existing deployment pipeline
to redeploy. No database rollback or credential rotation is needed.
