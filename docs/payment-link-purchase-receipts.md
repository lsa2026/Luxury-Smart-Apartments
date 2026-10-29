# Payment-link purchase coverage — MAIL-1a17b6cd6606f536

The existing HyperPay result verifies money server-side and routes payment-request
captures to a private result. That result previously returned before the ordinary
booking's purchase bridge. It must stay private: enabling Google on a bearer URL
would disclose its capability and ambient payment context.

New eligible returns redirect automatically to `/payments/receipt/`. A server-side
session grant identifies the verified capture; no payment ID/token is placed in
the destination URL. The redirect uses `no-referrer`. Incoming queries or referrers
on the clean endpoint trigger another clean hop before trackers render. The final
page uses `same-origin` so Safari's CSRF-protected acknowledgement still works.

The minimal receipt document includes only the existing measurement-privacy,
consent and analytics scripts and safe public property/receipt fields. It omits
guest details, account/header context, payment capability URLs, enhanced-conversion
email data, Meta, Clarity, consent banners and dialogs. Measurement metadata is
pinned to the clean receipt URL with an empty referrer. CSP retains the existing
Google destinations; no container or tag is created.

The existing `purchase`/`ecommerce` shape, `MarketingEventReceipt` ledger, HMAC,
signed acknowledgement, GTM callback and session-storage retry logic are reused.
`transaction_id` is the stable HMAC of `payment-link-capture:<internal UUID>`;
neither the UUID nor original booking reference is measured. `value`/`currency`
come from the actual verified capture (SAR), not the booking's new gross total.
Independent remaining-balance captures receive different transaction IDs.
`checkout_kind=payment_link` is optional metadata; GTM requires no change to fire
the existing purchase trigger. The ordinary booking bridge remains unchanged.

Only succeeded HyperPay captures with a verified timestamp, provider payment ID,
paid matching request and active real Hostaway reservation qualify. Refunds,
pending/review/failed outcomes and inconsistent amounts do not. Creation and
verification must both be on/after `2026-10-29T04:31:00Z`; old purchases are not
backfilled. The session grant expires after one day and eligibility is rechecked.
No historical result page is opened or event replayed in acceptance checks.

Consent defaults/options and `anonymousPurchase`/`loadPurchaseTracker` behavior
are unchanged: no new granted state or advanced-consent policy. Receipt EMITTED
means the matching GTM container processed the browser event and acknowledged it;
it does not prove Google's receipt or Ads attribution. Retries before a processed
acknowledgement retain the same transaction ID; after processing, the existing
client/server markers prevent replay. Blocked Google remains retryable as before.

Limits: this browser bridge needs the guest's return to the receipt, JavaScript
and a working existing GTM destination. Closing the browser before returning,
network/ad blocking or lack of Google-side evidence must not be called successful
measurement. Financial verification/reconciliation is independent and unchanged.

No schema migration, credential, consent setting, settlement, guest notification,
group-notice PR134, UAT resource, recurring task or production test is involved.
Rollback: revert this isolated change and redeploy the previous production commit
`fa60338fd42f520b920604b6fd2920bfcf088fab`. This restores the private result behavior
without changing financial history. Retain MarketingEventReceipt rows; never
delete their acknowledged markers or replay historical purchases on rollback.
