# n8n external booking loyalty invitations

Tracking: LSA-LOYALTY-N8N-20261029-0107-03

## Current status

Prepared and tested; NOT applied or activated in live n8n. Website PR 139 provides the coordination API. A dedicated verified credential in n8n is still required. No real guest tests, historical campaign, new workflow or parallel schedule.

## Existing workflow hooks

| Workflow | ID | Hook |
|---|---|---|
| 01 reservation journey | Pp0J8YZogO8AQfDc | after the existing booking delivery batch/report; read authoritative snapshot and receipts |
| 03 phone recovery | sNwA2n4FfnAMqFDW | after 120 Correction Result - End; only after successful core recovery |
| 06 Gathern | 0deZzThN5mrTS39o | after its existing booking batch and after successful 120 correction |

Essential messages already due must have delivery evidence to the current correct phone. Gathern email acceptance is never such evidence. Future entry/checkout reminders retain their current schedule. A missing number or failed core message holds the optional invitation until a qualifying correction event. Optional invitation failure cannot block essential messages already completed.

The builder adds 16 nodes inside an existing workflow and preserves existing nodes, paths, schedules, credentials and connections. It defaults to a closed entry gate. Its active configuration requires an explicit future UTC activation timestamp; only bookings created on or after that timestamp qualify. No historical sweep or backfill is performed. Website/direct sources and scenario 07 are excluded.

## Website v1 contract

Pinned in website-contract-v1.json from project 0107 PR 139. Actual routes live under /integrations/loyalty/invitations/: claim/, preflight/, record/, status/, plus authenticated GET health/.

- Claim sends exactly reservation_key, phone, email, language, guest_name, source_platform and event_id. A successful response requires code=ok. decision=eligible is authoritative eligibility, not permission to send; send_allowed must still be false. Match recipient_phone and preserve provider_reference.
- Preflight sends exactly invitation_id, ticket, phone. Only code=ok, decision=eligible, send_allowed=true with matching recipient_phone, invitation_id, provider_reference and a future send_expires_at authorizes a single provider POST. The generated HTTP URL also refuses to issue a POST after that deadline.
- Record sends exactly invitation_id, provider_reference, status, provider_id. Status distinguishes accepted/unknown/failed/delivered/read. Provider acceptance is not delivery. Exact provider id/reference/recipient evidence is required to advance delivery. No numeric ACK inference.
- Status sends invitation_id. A preflight timeout, ambiguous record acknowledgment or previous accepted/unknown invitation uses status rather than another provider POST. Unresolved states are held for attention.

The website owns atomic membership checks and the shared cross-channel/reservation/recipient ledger, including manual website invitations. The n8n test ledger is explicitly a test double, not proof of production durable idempotency. The website API never sends the n8n invitation. Enrollment, points, referral attribution and post-enrollment welcomes remain website responsibilities.

## Authentication

Dedicated credential name proposed by 0107: LSA Loyalty Invitations v1. API uses Authorization: Bearer with a dedicated invitation-only key. Do not reuse an unrelated Header Auth or Sama credential. New credential can use httpTemplatedCustomAuth with a Bearer header template; the builder also accepts an explicitly verified existing httpHeaderAuth binding. Never put the secret in Code, URLs, exported workflow JSON, repository content or chat. Bind existing Supabase and UltraMsg credentials by ID; do not copy their secret values.

Owner-only setup and readiness paths are documented by 0107 in the pinned OpenAPI fixture; their deployment and authenticated health must be verified before activation. No credential was provisioned by this n8n change.

## Content

Images remain the exact approved ar/en/fr PNGs from apps/loyalty/invitation_assets/enrollment-owner-review-v3.json at c01024ca156065ab31ae690fe59077eed9c9dbce. Tests verify SHA-256. Captions replace fictional names with the actual guest name, qualify the current-stay phrase for eligible website bookings, and explicitly state that the external booking earns no retrospective stay rewards. 3%/5%, Sama and the approved rewards join URL remain. Unsupported language and overlong captions are held rather than guessed or silently truncated.

## Verification

Run from repository root:

    node --test integrations/n8n/loyalty/*.test.cjs

38 offline tests pass. They cover languages/artwork, member suppression, missing/local/international numbers, repeated events, corrected numbers, old email acceptance, correct-recipient essential delivery, historical/ended/website bookings, timezone mapping, provider errors/timeouts, exact receipt checks, strict actual website request schemas, one-use preflight, status reconciliation and generated Code pairing/gates.

All 16 generated node parameter sets passed the native n8n schema validator. The sanitized inactive structural projection passed the skill helper with no errors. Generated Code ran locally against synthetic envelopes. Actual n8n runtime execution, real membership API access, shared production deduplication and deployment remain unverified until the dedicated credential is configured and the final patch is applied. No live guest is an acceptance-test recipient.
