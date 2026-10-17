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

## Owner-approved provisional wallet extraction

The payment project observed `walletProvider=APPLE_PAY` in the original
transaction's production connector log and `Payment Brand=VISA` independently.
The owner declined contacting HyperPay and explicitly approved adopting that
marker provisionally, with future Analytics validation rather than another
charge or historical purchase replay. The exact API position remains unverified.

Persist only the allowlisted normalized wallet in a new optional
`PaymentAttempt.payment_wallet` column. Inspect a root walletProvider field or
provider resultDetails, including serialized connector entries. Do not use
merchant customParameters, query arguments or selected UI tabs. Unknown,
missing, conflicting or unsupported markers yield an unknown payment_type
while preserving the reported network. Legacy paymentBrand=APPLEPAY remains
recognized. Never convert all VISA/MADA/MASTER payments into Apple Pay, and
never infer a direct card solely from the network.

This optional measurement field does not change payment acceptance, amounts,
currency, result code validation, financial reconciliation or receipt identity.
The migration is additive with no historical backfill. Existing purchases are
not reprocessed or resent. Arrival in future verified provider responses and
GA4 export remains a runtime validation item, not a completed assertion.

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

Owner approved publication with provisional wallet mapping and later checks;
no external contact or schedule is authorized by this decision. Deployment and
GTM publication must still be evidenced separately. Keep the existing backup.

References inspected:
- https://hyperpay.docs.oppwa.com/reference/parameters
- https://render.com/docs/deploys.md
- https://render.com/docs/health-checks.md
