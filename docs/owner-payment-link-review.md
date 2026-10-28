# Owner review of an amended booking payment link

After the owner approves the final stay total, the booking edit screen offers
an inactive payment-link review alongside the existing Aseel action. The review
shows the approved stay, recipient, total, verified funding, remaining balance,
SAR collection amount, stable URL and the existing branded guest message.

Preview does not update Hostaway, open a checkout or send a message. Explicit
confirmation updates/reconciles the same Hostaway reservation, checks the local
funding again, verifies remote collections, atomically activates the reviewed
balance and queues one guest delivery. A failed delivery does not undo a
confirmed Hostaway update; existing deliberate manual resends remain available.

Each version is a separate payment request. Original amounts, acceptance,
captures and delivery records remain attached to their original request.
Previously distributed tokens resolve to the approved successor; the stable URL
does not change. Inactive drafts never become payable. A guest form loaded on an
earlier version must display the approved new amount before checkout.

Checkout and success callbacks validate the approved reservation and funding
signature. The payment attempt always carries the requested SAR balance. A late
old success or a changed collection/refund/points state becomes review, never
permission for another collection. Existing cross-currency reconciliation guards
remain in place; an unpaid foreign-currency stay uses a frozen FX quote for its
balance, and its Hostaway receipt retains the source amount/currency.

Migration 0018 is additive: nullable associations and approval time, an inactive
status, and a JSON snapshot with a database default. The latter keeps requests
written by an old instance valid during rolling deployment. No settings,
credentials, schedules or live financial records are rewritten. A rollback can
retain these columns and history; do not remove active revised requests or
replay their deliveries. An old application version will refuse modified stays
rather than charge an old amount, so prefer a forward fix for an active revision.

Regression tests use disposable databases and fake providers. PostgreSQL CI
includes concurrent first approval and legacy INSERT compatibility. Production
verification is read-only: do not approve a real stay, send a guest message or
perform a payment as a deployment smoke test.
