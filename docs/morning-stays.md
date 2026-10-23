# Independent morning stays

Only booking management exposes **إنشاء حجز صباحي**. An authorized
operations user selects the apartment, one local calendar date, arrival after
00:00, departure strictly before 12:00, full guest name, international phone,
email, occupancy, and the approved all-inclusive **SAR** amount. Riyadh and
Marrakech use the apartment's configured timezone. No nightly date is invented.

`MorningStay` is a separate internal allocation, not a `Reservation`,
`BookingIntent`, or Hostaway booking. Creation only reads Hostaway. It requires
the previous-night calendar, complete paginated reservation coverage, no
overlapping guest/owner/pending stay, no unexplained blocking, and a verified
apartment mapping. The owner's immediate-turnover policy is used; the calendar
does not independently certify cleaning or unrecorded maintenance. Ambiguous
multi-unit inventory is held for human review. An afternoon arrival is allowed
when at least three hours remain after morning departure for turnover.

Requests are idempotent by UUID. A property row lock serializes internal morning
allocations, ordinary initial checkout claims, and ordinary Hostaway creation
claims. Ordinary availability checks include morning allocations. A five-minute
unpaid allocation expires no later than arrival. Pending, unknown, review, and
paid allocations never become available merely because that lease expires.
Provider-side channels and external/manual changes do not participate in the
local database lock: fresh remote reads reduce this race but cannot lock external
Hostaway inventory without creating a Hostaway booking.

The operator copies a signed, revocable-by-cancellation payment URL. Creation
and preview GETs send no guest message and create no provider checkout. The
guest supplies real billing details and explicitly accepts the current terms
and house rules (including smoking rules). The service rechecks availability,
serializes a durable payment claim, and uses the existing HyperPay payload,
MADA/Visa/Mastercard/eligible Apple Pay widget, and server-side result validation.
The separate `MorningPaymentAttempt` ledger cannot enter the ordinary booking,
Hostaway financial receipt, refund, loyalty, or invoice workflows. The approved
MorningStay communication integration reuses the existing n8n 01/02 engines.

The existing payment-recovery task also reconciles bounded morning attempts via
provider GET; no new schedule is introduced. Unknown checkout creation blocks
another charge and requires review. Verified money remains recorded even when
it arrives after the selected admission time; the stay then needs administrative
confirmation. Cancelling an unpaid request cannot override a pending/unknown
payment. No automatic refund or Hostaway write is performed.

## Guest communication

`MORNING_N8N_ENABLED` and the approved `MORNING_N8N_WEBHOOK_URL` enable future
events only. A MorningStay save records its outbox event atomically with the
business change. It requires production, stay status `paid`, and a matching
server-verified successful MorningPaymentAttempt. A successful charge with stay
status `review` does not grant admission or send instructions. No historical
paid stays are scanned or backfilled by enabling the switch.

The existing payment-recovery worker delivers pending events on its minute tick.
The POST carries only an event UUID and its signed event capability. n8n reads
the authoritative latest payload from the fixed website origin, and acknowledges
only after Supabase persistence. Lost responses retry the same event; monotonic
source revisions and stable message keys prevent duplicate jobs. A successful
HTTP POST alone does not prove guest delivery. Existing provider receipt checks
and uncertain-send reconciliation remain responsible for that evidence.

Workflow 01 stores the morning reservation with `morningstay:<UUID>`, uses the
same English-name Google Contacts label/match/merge process, and sends the paid
morning confirmation, existing house rules and apartment guide. Morning text
includes exact local arrival/departure times and no overnight/night-count
assumptions. Apartment code release still requires identity verification by
support; no nonexistent morning portal is promised. A single booking-status
job carries the Marrakech operations notice, with guest failure information
when necessary. Contact failure does not block booking messages.

Workflow 02 checks morning checkout reminders every minute and schedules them
60 minutes before the actual `ends_at`. When 60 minutes or less remain at
confirmation, departure is included prominently in the initial messages and no
separate immediate checkout reminder is queued. Normal overnight 21:00 paths
remain unchanged. Minute dispatch uses `lsa_morning_next`, which selects only
MorningStay jobs and cannot wake unrelated historical booking jobs.

Paid bookings expose an operations edit screen for guest identity, phone,
language, apartment and the exact stay window. Inventory is rechecked for window,
apartment or occupancy changes; an optimistic version check rejects a stale
admin submission. An ongoing stay can retain its original arrival while changing
its future departure. The verified paid amount is preserved. Changes update the
same reservation and future reminder, invalidate obsolete pending jobs, and send
an updated status. Apartment/arrival/phone changes refresh the access guide;
phone changes include house rules for the corrected recipient. Stable completed
rule/guide keys prevent ordinary metadata changes from repeating them.

Morning correction links open the internal operations screen, never workflow
03's Hostaway PUT path. They use the same contact merge and delivery engine while
keeping the source record authoritative. Cancellation keeps booking/payment
history, stops pending reminders and messages, and sends one cancellation status
and operations notice. It never deletes a contact or automatically refunds money.
Already in-flight provider requests cannot be recalled; their evidence remains
available for review after a revision supersedes them.

The applied Supabase migration is `morning_stay_guest_journey_20261023`.
`docs/automation/morning-contract-check.sql` tests invalid payment, durable duplicate
ingestion/enqueue, same-day phone changes and cancellation in a subtransaction
that always rolls back synthetic data. Node configuration validation and
`node scripts/automation/test_morning_patch.cjs` check the actual workflow Code
changes without credentials or network access. The patch JSON contains no
credential bindings; production nodes reuse their existing Supabase credentials.

Validation uses synthetic data and fake provider clients in
`tests/test_morning_stays.py`, including PostgreSQL concurrency in the existing
quality workflow. No live booking, payment, guest send, or production test record
is required to validate the release.
