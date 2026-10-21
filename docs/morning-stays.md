# Independent morning stays

The operations hub and booking list expose **إنشاء حجز صباحي**. An authorized
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
Hostaway financial receipt, refund, loyalty, invoice, or guest-message workflows.
Those integrations are not silently claimed for an independent morning stay.

The existing payment-recovery task also reconciles bounded morning attempts via
provider GET; no new schedule is introduced. Unknown checkout creation blocks
another charge and requires review. Verified money remains recorded even when
it arrives after the selected admission time; the stay then needs administrative
confirmation. Cancelling an unpaid request cannot override a pending/unknown
payment. No automatic refund or Hostaway write is performed.

Validation uses synthetic data and fake provider clients in
`tests/test_morning_stays.py`, including PostgreSQL concurrency in the existing
quality workflow. No live booking, payment, guest send, or production test record
is required to validate the release.
