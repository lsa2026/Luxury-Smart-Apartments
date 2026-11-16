# Independent administration stay extensions

This journey creates a new direct reservation. It never changes the original
channel reservation, its dates, totals, collections or cancellation policy.
Only existing operations access can select a source, prepare the draft or create.

The selector reads current reservations across Hostaway channels with cursor
pagination. Filters are applied to each page, including empty filtered pages;
the interface keeps the next source page available. Selection and final creation
re-read the source. Morning/day-use markers, providers and local ownership are
excluded because those journeys retain their own inventory and messaging rules.
Gathern source metadata remains visible with an explicit temporary block until
receiver health reports `gathern_guard_ready=true` alongside overall readiness.
This is a server-side capability, never a browser form option. Its independent
`lsa06_*` sender, correction and review queues must be tested and their published
guards verified first. This release does not publish unrelated automation drafts.
The n8n registry also checks its exact canonical Gathern reservation key before
acknowledging ownership. Cached display readiness lasts at most ten seconds;
pre-create readiness and exact ownership acknowledgement remain mandatory.

Guest corrections and approved billing belong to the new draft/intent only.
Both are completed before the initial availability/price check and carried into
the draft for review. An unavailable quote retains the bound guest/address form.
Shared address suggestions, HyperPay validation and international phone rules
are reused. Guest terms/house-rule consent is collected by the existing payment
page. Administrative data entry does not substitute for the guest's consent.

## Creation and collection

One open extension per parent and a property row lock prevent concurrent drafts.
An explicitly requested replacement after verified cancellation keeps the old
extension/history; it never retries an uncertain provider operation.
The local intent, child reservation and relation exist before a provider POST.
Creation requires n8n readiness and an exact ownership ACK of the current chain
revision. The provider note is `EXT01:{extension UUID}`; the note alone is never
ownership proof. An early authenticated webhook may bind only the ACKed planned
child whose listing, dates, direct channel, currency and total all match.
The established payment-link path and production gateway must be enabled and the
approved total must be positive before creation; this journey adds no free or
unpaid activation exception.

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

The website owns one combined booking-confirmation/payment-link message for a
verified child booking: additional dates, actual new checkout in the property's
timezone, amount/currency, secure payment link and an explicit awaiting-payment
status. The existing logo, AR/EN/FR language and durable delivery row are reused.
The financial receipt is unchanged. Generic child confirmation stays suppressed.
For extensions only, n8n sends no second activation confirmation or house/check-in
instructions; it retains checkout and reviews under the existing paid-stay policy.
Linking creates no marketing consent, loyalty membership or extra invitation.

Delivery identity remains `(request_id, kind, sequence)` under
`unique_payment_request_delivery`; default calls return the existing row, receipts
are always single, and the provider reference remains `payment-request:{delivery.pk}`.
Unknown/sending outcomes never authorize another automatic POST. A deployment
does not replay old links or create delivery rows for historical extensions.
The extension health response adds
`extension_payment_message_contract=confirmed_booking_link_v1` without changing
communication contract version1. Publish n8n's `extension_payment_only_v2` policy
before this website message. Other booking, revised-payment, morning07/day-use08
messages and financial activation rules are unchanged.

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
Every extension context of one chain returns the same approved contact: the
latest non-cancelled prepared/creating, created-unpaid or active member. Partial
drafts never replace it; cancellation restores the preceding eligible contact,
or the last manager-verified correction if none remains. Contact source is exposed
as `communication_contact_extension_id`; event identity remains unchanged. The
contact and member decisions share the global fingerprint. A concurrent revision
change causes a bounded fresh read and then a hold if the chain keeps changing.

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
