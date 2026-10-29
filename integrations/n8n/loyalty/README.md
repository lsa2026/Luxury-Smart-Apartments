# External booking loyalty invitations — n8n integration

Tracking: LSA-LOYALTY-N8N-20261029-0107-03

Status: pure policy and offline tests prepared. Not activated. Website membership/atomic invitation-claim API and its credential binding are a required dependency requested from project 0107. No guessed endpoint, unauthenticated member lookup or production send is permitted.

## Existing workflows and event ordering

- Scenario 01: Pp0J8YZogO8AQfDc. Hook after the booking delivery batch finishes, using its existing reservation report scope. Only consider external bookings whose currently due essential WhatsApp messages have delivery evidence.
- Scenario 03: sNwA2n4FfnAMqFDW. Hook after 120 Correction Result - End, independently of contact bookkeeping. Read the current authoritative booking and delivery records after correction; never use the form payload as the booking source.
- Scenario 06: 0deZzThN5mrTS39o. Hook after the booking batch, and after 120 Correction Result - End for corrected Gathern numbers. Guest email sending remains disabled.
- No new workflow, no parallel schedule, no replay/backfill. Scenario 07 is website-only and excluded.

The activation timestamp must be written once when the live website API and authentication are verified. Only bookings created on or after this timestamp qualify. A later phone correction of such a booking can unlock its invitation after all currently due essential messages finish. Historical reservations cannot be swept into a campaign by an update or correction.

Confirmation, house rules, currently due entry instructions and correction of pending essential messages finish before invitation processing. Future entry/checkout reminders retain their existing schedule. Optional invitation failure must never block or downgrade core work.

## Website contract requested from 0107

This is a proposed adapter contract, not a claim that any endpoint exists. Confirm final paths, authentication and response schema with the website implementation before wiring HTTP nodes.

1. Atomic claim accepts reservation_key, phone, guest_email, language, guest_name, source_platform, booking_created_at, event_id and template_version. It authoritatively checks verified phone membership and a shared ledger covering website/manual/email/WhatsApp invitation paths. It returns member, duplicate, blocked, phone_missing or eligible. Eligible explicitly includes member=false, invitation_id, phone, stable reference_id and a short-lived ticket. A read error is attention, never non-member.
2. Preflight rechecks membership and target binding immediately before provider POST, and commits the invitation into a state that concurrent duplicate events cannot send. It returns allowed/member, invitation_id, phone, reference_id and expires_at. New membership, cancellation, changed target, expired ticket or unverified response must deny sending.
3. Record stores accepted, unknown, failed, delivered and read distinctly. A timeout after POST is unknown and must never cause another blind POST. Exact provider id/reference/recipient evidence may reconcile it. Automatic changes of phone or channel cannot create another invitation for an already accepted/ambiguous reservation. Shared recipient/account uniqueness spans different reservations and both invitation senders.
4. Record/status endpoints and durable alerts must recover recording failures without sending the invitation twice. Authentication is supplied via a credential binding, never in Code, URLs or repository content. No member data listing endpoint is required.

The website owns membership and welcomes after enrollment. n8n invitation processing performs no enrollment, points, referral attribution or welcome write.

## Approved content

Pinned asset source: apps/loyalty/invitation_assets/enrollment-owner-review-v3.json at c01024ca156065ab31ae690fe59077eed9c9dbce. Images are unchanged and their SHA-256 is verified in tests. Captions replace only the fictional guest name and qualify the current-stay wording for external bookings; 3% and 5% apply only under eligible website booking terms, with no retrospective reward for external stays. The Sama paragraph and the approved join URL are preserved.

Language comes from existing guest language selection. Unknown language is skipped for attention, not guessed from an unrelated contact. Overlong captions are held rather than silently truncated. Names cannot insert markup or invisible control characters. Content version: external-enrollment-v1-20261029.

## Checks performed

Run from the repository root:

    node --test integrations/n8n/loyalty/*.test.cjs

33 offline tests pass: three language/image/caption fixtures, fresh external booking after core delivery, disabled/historical gate, website and ambiguous source exclusion, missing/invalid/local/international phone, old email acceptance, pending core work and correction, entry instructions still pending, existing member, duplicate, unavailable membership, changed target, joining during preflight, expired ticket, provider error/timeout, exact delivery evidence, caption size and shared-claim contract simulation.

The shared-claim Map in the test is explicitly a test double, not durable production deduplication. Atomic uniqueness, real membership lookups, credential binding, n8n node execution and deployment need evidence from the actual website API before activation. No real guest is a test recipient.

## Additive workflow patch builder

`build-patch.cjs` generates 15 additional nodes inside each existing workflow, without deleting, renaming or changing existing nodes or schedules. It requires explicit final website URLs and credential references. Its default entry gate is closed; an enabled build requires an explicit activation timestamp. The website key is a dedicated credential (prefer `httpTemplatedCustomAuth` with a Bearer-header template), never an embedded value. Existing UltraMsg and Supabase credentials are referenced by ID rather than copied.

The generated node parameters passed the native n8n schema validator (15/15). A sanitized inactive structural projection passed the skill helper with no errors. Generated Code was executed locally with synthetic HTTP envelopes, including filtered multi-reservation input pairing and the closed entry gate. This is not evidence of execution in the actual n8n runtime, and no live workflow has been patched or published for this feature yet.

Project 0107 acknowledged the dependency and is implementing claim/preflight/record/status under `/integrations/loyalty/invitations/`. The exact final JSON and dedicated credential setup must still be verified before applying any generated patch. The mock URLs under `__contract_fixture__` exist only in offline fixtures and must never be used for deployment.
