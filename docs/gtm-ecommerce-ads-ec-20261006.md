# Google Tag purchase and apartment journey update — 2026-10-06

## Scope and release contract

Owner-authorized Google Tag/site changes only. Ads action/account settings are
coordinated separately. Do not fabricate an ad click or replay past purchases.

### Apartment ecommerce

`analytics.js` v18 clears `ecommerce` before every accepted event and supplies
sanitized event-local items/value/currency/list name. Only purchase is allowed
transaction_id/tax/coupon. Consent and existing receipt/deduplication remain intact.
Enable **Send Ecommerce data / Data Layer** on `GA4 — Guest journey events`;
keep its trigger regex excluding purchase and its five property/contact parameters.

### Ads-only purchase identity

The authenticated/owned or signed-return payment result prepares a SHA-256
email fingerprint only with a verified purchase and the current cookie's
independent UPD + analytics + marketing permission. Gmail/Googlemail local-part
dots are normalized. Raw identity is not placed in the purchase, dataLayer,
tracking receipts, logs, or browser storage. Response is no-store/private.

`ads-purchase-data.js` runs before analytics, removes the temporary DOM attribute,
and provides `window.LSAAdsPurchaseData.read()` only to a dedicated Ads variable.
It returns an empty object without permission or identity, and permanently clears
in-memory identity after withdrawal/pagehide. The existing GA4 UPD accessor is
unchanged. Missing identity never blocks the ordinary purchase tag.

GTM variables to create:

- `JS — Ads purchase consented email` (Custom JavaScript):
  `function(){return window.LSAAdsPurchaseData && window.LSAAdsPurchaseData.read ? window.LSAAdsPurchaseData.read() : {};}`
- `UPD — Ads purchase email only` (User-Provided Data, Code), referencing that JS.

On **Google Ads — Purchase — Verified**, add event parameter
`user_data={{UPD — Ads purchase email only}}`. This event-specific parameter
keeps identity off the shared Google tag and GA4 purchase tag. Retain Conversion
ID `18090693214`, label `N6xwCP_Tr7McEN6kqLJD`, transaction/value/currency variables,
and the existing purchase trigger. No additional purchase or lead conversion tag.

## Value policy

The purchase sanitizer already rejects missing/zero/negative/non-finite value,
missing transaction ID, and malformed currency. Such a record emits no purchase
and does not acknowledge a receipt. Do not replace unknown revenue with 0/1.
The Ads UI's zero-SAR fallback is a last-resort account setting, not evidence
that an unknown amount is truly zero. Investigate any fallback usage against
verified booking/payment records; keep it visibly separate from known revenue.

## Verification and limits

Synthetic local tests cover ecommerce reset/scoping, no purchase duplicates,
invalid value rejection, denied consent, independent opt-in, withdrawal,
hash normalization, and result-page script order. No real payment, historical
purchase replay, or synthetic identity is sent to Google by these tests.

Implementation success is not proof of Google ingestion, Ads attribution,
or reporting accuracy. Verify actual outgoing `em` only after an owner-authorized
new booking with opt-in; verify a real ad-attributed booking when one occurs.

References:

- https://developers.google.com/analytics/devguides/collection/ga4/ecommerce?client_type=gtm
- https://support.google.com/google-ads/answer/13262500?hl=en-GB
