# n8n external booking loyalty invitations

Tracking: LSA-LOYALTY-N8N-20261029-0107-03

## Current status

Prepared and applied as CLOSED staging to existing n8n 01/03/06 workflows; invitation sending is NOT activated. Website PR 139 is merged and its contract at eb526e7c366c73239513a7a8c3a8dead1cf4f925 is byte-identical to website-contract-v1.json. A dedicated verified credential in n8n is still required. No real guest tests, historical campaign, new workflow or parallel schedule.

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

40 offline tests pass. They cover languages/artwork, member suppression, missing/local/international numbers, repeated events, corrected numbers, old email acceptance, correct-recipient essential delivery, historical/ended/website bookings, timezone mapping, provider errors/timeouts, exact receipt checks, strict actual website request schemas, one-use preflight, status reconciliation and generated Code pairing/gates.

All 16 generated node parameter sets passed the native n8n schema validator. The sanitized inactive structural projection passed the skill helper with no errors. Generated Code ran locally against synthetic envelopes. Exact closed-gate code also passed isolated native n8n runtime tests 36478 (01), 36479 (03) and 36480 (06), with every external service pinned and no downstream execution. Temporary manual test nodes were removed. Real membership API access, the active end-to-end invitation graph, shared production deduplication and sending remain unverified until the dedicated credential is configured and authenticated health passes. No live guest is an acceptance-test recipient.

## Closed staging and activation handoff — 2026-10-29

Staging uses `staged:true`, `enabled:false`, `activation_at:null`. L00 is an enabled Code gate that returns zero items before reading inputs or services; the other 15 optional nodes are disabled. The four website API nodes deliberately have no credential binding and `authentication:none` only while disabled, preventing accidental auto-assignment of an unrelated Header Auth credential. This is not a credential alternative or an active anonymous API integration. Do not enable those nodes in that state.

Reviewed against live workflow baselines: 01 active 83f6dfba-07d0-49fb-90d3-6792f83abb65, 03 active 4f91e561-15ff-4214-a628-aedfe3f6e84a, 06 active 52af5373-7322-43b9-be13-5d7675c5d908. The original nodes, settings and edges are unchanged. In particular the 20:55–21:05 reminder scan loop, its 30-second waits, and disabled Gathern guest email nodes remain intact. No reminder hook, new trigger, parallel scheduler, manual catch-up or historical sweep was added.

After the owner provisions **LSA Loyalty Invitations v1**: read its metadata ID/type only; verify authenticated GET health with that binding and require ready/programme_ready. Then generate the fully bound configuration (not staged), choose and document a future UTC activation_at, update existing L00/L03 config and the four API authentication/credential bindings, explicitly enable the 15 existing optional nodes, and run pinned isolated acceptance before publishing. Do not reapply addNode operations to already staged IDs and do not issue any guest/provider test POST. Invitation sending is not authorized before these prerequisites succeed. The remaining known external blocker is the missing dedicated credential; its validity cannot be asserted until authenticated health succeeds.
