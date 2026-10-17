# Measurement URL privacy — 2026-10-17

Owner authorized tracking-only repairs and publication on 2026-10-17, with no
new payment, purchase replay, refund, or change to payment collection.

## Scope of this draft

`measurement-privacy.js` queues global `page_location` / `page_referrer`
metadata before deferred Google loading. Consent and event emission refresh
the same metadata. Standalone GA4 configuration explicitly receives it too.
Private payment/booking paths lose object identifiers, query strings and
fragments in measurement metadata only. Public campaign parameters remain;
unknown query parameters are omitted. The real location, provider form,
return-token verification, transaction identifier, amount, consent and purchase
receipt/acknowledgement logic are unchanged.

The script does not transmit anything, change browser history or grant consent.
Meta keeps its separate public-page/referrer restrictions unchanged.
The ordinary HyperPay result response also sets `Referrer-Policy: no-referrer`
to prevent the functional return URL leaking through HTTP referrer headers.

## Important incomplete work

This is NOT a completed Apple Pay classification fix. Existing code stores only
the provider's `paymentBrand` and infers `payment_type` from that brand. A Visa
Apple Pay payment can therefore be emitted as card/visa. Do not replace all
Visa payments with Apple Pay, trust a query parameter, or treat a selected tab
or card-form submission callback as proof of wallet settlement.

The payment project was requested by the owner to review the authoritative
response contract, read-only, on 2026-10-17. Request-only fields documented by
HyperPay (such as `applePay.source`) do not establish that the corresponding
field is present in this account's verified response. No raw provider response,
card data, credentials, guest data or payment URL tokens are recorded here.

## Validation and release gate

- Focused Django purchase/payment/marketing tests: 204 passed before final
  template-order assertions; rerun affected tests for the final revision.
- Final focused Django payment/consent/Meta/URL-header revision: 95 passed.
- All Node tests in this revision: 173 passed (48 focused tests also passed).
- Ruff lint/format and migration drift checks passed.
- Full local Python suite at be9552b: 3526 passed, 14 skipped (SQLite cannot
  exercise the PostgreSQL row-lock cases). Final HTTP-header revision is covered
  separately by the 95 focused tests. These are not production runtime proofs.
- New tests use synthetic values only and make no payment or measurement calls.

Global Google metadata is not proof that every Ads request honors an override.
Before calling URL privacy complete, inspect the actual destination output in
an authorized non-purchase runtime test. Never send a fabricated conversion or
replay the owner's existing purchase to validate this. Preserve any unresolved
destination behavior as an explicit release blocker.

Publication of the combined repair/upgrade remains pending those gates and a
verified wallet source. No deployment or GTM publication is implied by this
draft. Existing backup and upgrade workspace remain intact.

References inspected:
- https://hyperpay.docs.oppwa.com/reference/parameters
- https://render.com/docs/deploys.md
- https://render.com/docs/health-checks.md
