# Admin-only MorningStay recording in Hostaway

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
