# Independent administration stay extensions

This journey creates a new direct reservation. It never changes the original
channel reservation, its dates, totals, collections or cancellation policy.
Only existing operations access can select a source, prepare the draft or create.

The selector reads current reservations across Hostaway channels with cursor
pagination. Filters are applied to each page, including empty filtered pages;
the interface keeps the next source page available. Selection and final creation
re-read the source. Morning/day-use markers, providers and local ownership are
excluded because those journeys retain their own inventory and messaging rules.

Guest corrections and approved billing belong to the new draft/intent only.
Shared address suggestions, HyperPay validation and international phone rules
are reused. Guest terms/house-rule consent is collected by the existing payment
page. Administrative data entry does not substitute for the guest's consent.

## Creation and collection

One extension per parent and a property row lock prevent concurrent drafts.
The local intent, child reservation and relation exist before a provider POST.
Creation requires n8n readiness and an exact ownership ACK of the current chain
revision. The provider note is `EXT01:{extension UUID}`; the note alone is never
ownership proof. An early authenticated webhook may bind only the ACKed planned
child whose listing, dates, direct channel, currency and total all match.

The existing provider operation permits at most one POST attempt. An uncertain
response is reviewed; it is never retried as a new reservation. After a provider
attempt, rechecking the draft cannot replace its quote and create a second intent.
If preflight remains unresolved past quote expiry, review is required; do not
create a replacement draft to bypass the operation's history.

Initial payment-link sending uses the existing idempotent queue. Explicit later
resends remain in the booking's established administration screen. A paid label
alone cannot activate an extension: a matching verified production HyperPay
collection or the existing full manual collection proof is required. Testing
environment payments do not activate live stay communications.

## Communication ownership

The website retains payment links and financial receipts. n8n owns the dedicated
activation confirmation and existing receipt-aware instructions, checkout and
reviews. Generic child booking confirmation is suppressed. Linking creates no
marketing consent, loyalty membership or extra invitation.

The website advances the final checkout only through contiguous, verified paid
members. An unpaid child leaves the existing checkout owner unchanged. A cancelled
terminal child restores the previous connected member; an active member beyond
a cancelled gap requires review and blocks communication. Never take the maximum
checkout across unrelated or disconnected reservations.

`GET /reservations/stay-extensions/{UUID}/context/` requires a scoped signed
capability in `X-LSA-Extension-Token`, never a URL query. It returns current state,
member decisions and a limited communication snapshot without email, address,
payment details, door code or raw provider response. GET does not mutate business
or financial state. It performs bounded exact-ID provider reads; unavailable or
changed state prevents related messages. Unrelated bookings remain unaffected.

An event capability proves its persisted event ID/revision/kind while reading the
latest chain. An older valid capability remains useful for later guards. Fresh
events renew the capability through the connected stay plus 90 days. Original
loyalty/invitation credentials are not reused.

Event ACKs match event ID, extension ID, source revision and contract version.
`delivery_pending=true` leaves the same event retryable until immediate work for
that chain finishes; future reminders and separately recorded attention do not
keep it open. Transport retry never authorizes a second provider message.
The durable outbox normally commits with the booking/collection change; projection
failure cannot roll back a verified collection. The existing 60-second payment
recovery task handles up to three pending extension entries per round, with
20-second transport timeouts and ten automatic attempts before review. No new
schedule or old booking backfill is created.

Property checkout policy and named zones are preserved. The narrow Morocco UTC
compatibility rule matches the existing automation and
[IANA tzdb2026c](https://lists.iana.org/hyperkitty/list/tz%40iana.org/thread/NVHSX2PAQIT44U5FCCEVNJJYXQMMTJSA/),
including hosts with older timezone data. Other website journeys are not changed
by this compatibility logic.

## Deployment acceptance

Both SQLite and PostgreSQL suites, concurrent-parent creation, exact financial
proof, early webhook binding, uncertain POST, old capabilities, cancellation gaps,
scoped delivery recovery, shared phone/address forms, and phone/desktop light/dark
synthetic layouts must pass. The fixture artifact contains synthetic context
snapshots with placeholder capabilities for independent n8n contract validation.
No live guest booking, collection or message is part of acceptance. Activation
requires the separate n8n implementation's verified readiness and matching guards.
