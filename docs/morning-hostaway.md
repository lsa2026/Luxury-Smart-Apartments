# Admin-only MorningStay recording in Hostaway

## Current creation flow — manager confirmation before payment

The private create page requires the complete guest address, shared phone parser,
name, email and explicit manager confirmation. It rechecks availability, saves the
local allocation and creates Hostaway immediately. New allocations carry
`creation_mode=hostaway_first_v2`; older independent stays keep their original policy.
Guest phone, email, locale and supported address fields are included in the
Hostaway request. No card details or collection are sent. Manager identity and
the provider creation claim are durable before the non-retried POST.

Only a matching successful Hostaway response admits the booking into Morning07.
Its backward-compatible version 2 event carries `booking_verified=true`, a positive
Hostaway ID and `status=confirmed`, separately from truthful payment state. The
existing Morning07 journey owns confirmation, rules, instructions, contacts and
the reminder at the actual morning checkout time. Pathway01 continues to exclude
this source. Payment alone does not repeat booking messages.

After collection, a bounded task completes only the optional paid invitation via
the same Morning07 invitation helper, exact core delivery receipts and shared
registry. It does not re-emit the booking event. Bank/cash proof must be the
manager's explicit amount-matched receipt; no gateway success is invented.

After guest delivery progress, Morning07 requests the protected payment-link
callback with its current booking capability. A durable per-stay intent waits
for exact delivered/read receipts for status, house rules and access guide.
Only then does the existing sender revalidate the linked booking and send one
branded payment link. Provider acceptance alone is insufficient. Duplicate
callbacks, prior manual sends, uncertain outcomes, paid/cancelled/review states
and stale revisions cannot cause another automatic send. Receipt checks retry
at 30-second intervals for at most five minutes, scoped to this booking; no new
schedule or historical replay. The administration screen exposes waiting status.

For this additive callback, deploy the website/worker endpoint first, verify
both are live, then publish Morning07's two-node callback branch. No SQL or
Django migration, environment change, or real guest send is needed for release.

Confirmed and uncertain Hostaway claims retain local occupancy after the short
unpaid lease. Checkout/payment-link availability excludes only the exact linked
Hostaway ID after checking its source, listing, dates and active status. Other
reservations still block, and a missing/cancelled linked record blocks payment.
Availability refreshes preserve the linkage and creation mode.

Deploy the compatible Morning07 SQL/workflow first, then the website after CI.
There is no historical backfill or new schedule. Verification uses synthetic
provider fakes; do not create real bookings or send guest messages as a smoke test.
On rollback preserve provider IDs, financial records and uncertain claims; do not
retry uncertain creation or delete an external reservation automatically.

The following records describe the earlier paid-only recording release.

## Verified provider capability

On 2026-11-08 at 02:50 Asia/Riyadh, a direct API reservation was created
for 2026-11-07 to 2026-11-08 (Hostaway ID 71383026, n8n execution 37187).
Hostaway returned HTTP 200, success, status new. It was immediately cancelled,
then independently read as cancelled. No guest contact was supplied.
The provider replaced totalPrice=0 with the nightly rate, SAR665.

A second synthetic creation (71383136, execution 37194) confirmed that explicit,
overridden baseRate financeField details preserve SAR1 exactly. It too was
immediately cancelled. The calendar was independently confirmed available
after both cancellations (execution 37199). No payment or charge was created.
The temporary diagnostic in Saeed Projects was never published and was archived;
all booking write nodes were removed before archival.

These checks used the account's saved n8n Hostaway OAuth credential, not the
website's own token. The website continues to use its existing HostawayClient.

## Website behavior

Only the private MorningStay detail screen exposes recording, by an explicit
CSRF-protected POST after full payment is recorded in the website. Normal public
booking date validation remains unchanged. A morning that has just begun may
be created by management while its checkout is still in the future.

The stored morning window stays truthful. Only Hostaway's calendar reservation
uses the preceding night; hostNote retains the exact actual arrival, departure,
timezone and unique MorningStay UUID. The approved SAR amount is sent as an
explicit overridden baseRate and verified against the returned reservation.

Fresh Hostaway calendar and paginated reservation reads are required. The
existing property/stay lock order serializes recording; a durable creating claim
is saved before POST. A timeout is unknown and never retried automatically.
A differing response retains the provider ID for review rather than creating
another reservation. The linkage is retained under availability_evidence:
hostaway_morning. No migration or historical booking backfill occurs.

No card, charge, bank receipt, phone or email is sent to Hostaway by this
recording action. The actual payment stays in the existing website ledger.
Guest communication continues through independent pathway07. Pathway01 excludes
source LSA_MorningStay so the mirrored booking cannot trigger a second journey.

Linked stays cannot be edited independently of Hostaway. Cancellation must first
be confirmed in Hostaway; only then is the local stay cancelled. A timeout keeps
the local stay active for review. This explicit cancellation is restricted to
the dedicated source and direct channel and does not enable automatic guest
cancellation globally. There is no automatic refund.

## Validation

Tests cover actual 05:00 morning timing, previous-night mapping, occupancy,
authorization, POST-only writes, exact price, duplicate submissions, uncertain
creation and cancellation. All application tests use fake provider clients.
The existing quality gate runs these on SQLite and PostgreSQL.
