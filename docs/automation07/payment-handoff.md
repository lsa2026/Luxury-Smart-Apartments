# Morning07 independent payment handoff — PREPARATION ONLY

This branch is not published. No production settings, migrations, workflows or guest messages have been changed. Existing booking messages and automatic fourth payment link remain separate.

## Wire contract

Opt-in settings: `MORNING07_PAYMENT_HANDOFF_ENABLED` (default false), `MORNING07_PAYMENT_WEBHOOK_URL` (default empty). Existing `MORNING07_N8N_ENABLED` is also required. Planned n8n webhook: `/webhook/lsa07-morning-payment-v1`, within existing workflow07, no new scenario.

Webhook POST body contains only `payment_event_id` and `event_token`. Persist idempotently and acknowledge HTTP200/202 with `{"accepted":true,"payment_event_id":"same UUID"}`. Repeated delivery of the same envelope must never recreate jobs. The independent outbox has bounded retries; exhausted pending records need an operator retry of that event, never bulk history replay.

GET `/integrations/morning07/payments/events/<UUID>/`, header `X-LSA-Morning07-Payment-Token: <event_token>`. Returns verified source facts, stable payment UUID/revision, booking_revision, amount_sar (decimal string), SAR, proof_kind/proof_key, Hostaway id, apartment, guest/contact/language, actual timestamps/timezone, plus `payment_token` and `jobs` map. No raw HyperPay transaction id, card data, arbitrary message text or integration credentials. Capabilities expire after15 days; read and delivery capabilities use distinct signing purposes and are not interchangeable. Never log or persist the tokens in public artifacts.

POST `/integrations/morning07/payments/send/`, same header with `payment_token`. Exact body: `{"message_type":"payment_success","job_key":"lsa07pay:<UUID>:payment_success","reference_id":"lsa07pay-<md5 of job_key>"}`. Second kind is `loyalty_invitation`. Use values returned by `jobs`; do not provide message_body, phone or an event override. Success text is rendered on the website using actual local stay hours and approved support settings. Loyalty uses the existing image/caption/registry. The image's real provider reference is `lsa-loyalty-invite:<registry UUID hex>` and is returned separately from the functional reference_id.

Send results: `code`, `sent`, `state` when claimed, `reference_id`, nullable `provider_message_id`, nullable `provider_reference`. `accepted` is provider acceptance, never proof of delivery. `duplicate` must not trigger another POST; reconcile its returned state by receipt. `core_pending`, `payment_notice_pending`, `receipt_lookup_uncertain` before a claim are safe to defer30 seconds. `member`/registry `duplicate` skip invitation. `unknown`/`posting` require receipt reconciliation or review, never a second provider POST. Other eligibility/program errors need attention; do not blindly retry.

GET `/integrations/morning07/payments/receipt/?reference_id=<functional reference_id>` with `payment_token`. Only a reference owned by that event is permitted. Returns exact-filtered `messages` with id/referenceId/ack/status; ack2/3/4 or delivered/read/played proves guest delivery. Provider `sent`/ack1/server does not. HTTP503 means uncertain lookup. Receipt reconciliation remains allowed after a subsequent cancellation, but new sends require current paid proof, confirmed Hostaway, matching frozen source/recipient and a future checkout.

Order: three real core guest receipts -> fourth payment link (existing) -> independently verified collection -> success message -> actual success receipt -> loyalty image -> actual image receipt. Payment failure/cancellation/pending/unknown/review emits no financial event and no failure WhatsApp. No payment-only booking replay. n8n07 owns its independent financial queue; workflow04 owns reviews. Reviews need no additional website fields in this contract.

## Isolated phased test bridge

Run `../.venv/Scripts/python.exe scripts/test_morning07_payment_phases.py` from this worktree. The process creates an in-memory test DB, blocks HTTP, uses fake Hostaway/WhatsApp and emits one JSON object per input line after `{"ready":true,"synthetic":true}`. Keep one process alive across phases. It accepts:

```json
{"action":"source"}
{"action":"send","message_type":"payment_success"}
{"action":"acks","values":{"core":2}}
{"action":"send","message_type":"payment_success"}
{"action":"send","message_type":"loyalty_invitation"}
{"action":"acks","values":{"payment_success":2}}
{"action":"receipt","message_type":"payment_success"}
{"action":"send","message_type":"loyalty_invitation"}
{"action":"acks","values":{"loyalty_invitation":2}}
{"action":"receipt","message_type":"loyalty_invitation"}
{"action":"stats"}
```

Only these synthetic receipt controls are test-only. They are not deployed routes. Expected stats after duplicates: text_posts1, image_posts1, booking_events1, payment_events1. Source tokens are ephemeral test capabilities signed by the test-only secret. Never connect this bridge to a real provider/database.

Add `--full-journey` to begin with the confirmed unpaid booking, no seeded core attempts, no checkout and no financial event. Extra actions: `booking_source` reads the real booking event gateway; `booking_send` with `job` accepts the existing complete morning07 job body generated by n8n; `booking_receipt` with `reference_id` queries its real receipt gateway. `payment_link_request` calls the existing callback, and `payment_link_complete` runs its bounded completion step with fake Hostaway/WhatsApp. `verified_payment` runs real checkout preparation and server verification using HyperPayStub (optional outcome success/failed/cancelled/pending/unknown/review). A successful verification creates the financial event through the same production hook. `source` before success returns409/no_payment_event. Additional stats distinguish core_posts, payment_link_posts, hostaway_creates, checkout_posts and payment_checks. The complete successful guest sequence yields3,1,1,1,1,1,1,1,1 across core/link/text/image/booking/payment/Hostaway/checkout/check counts. Mock provider delivery proves contract behavior only, not real-world delivery.

## Future controlled activation (requires owner's review first)

1. Review and approve website patch and inactive n8n07/04/SQL artifacts together. Recheck live versions before deployment; no active workflow is edited during preparation.
2. Deploy migration/web/workers with the new flag still off. Drain/stop all old website workers and in-flight legacy invitation sends before enabling the new ownership. Workers running old code do not know the new guard. Verify they are gone and reconcile ambiguous old deliveries first.
3. Install/test financial queues and activate reviewed07 endpoints while website flag stays off. Verify exact source reads, idempotent persistence, token confidentiality, no general journey replay, review cancellation behavior and receipt filtering.
4. Enable financial handoff only after the above. New successful receipts claim durable event ownership; global flag blocks legacy invitation tasks/endpoint, and persisted payment_events keep those stays blocked even if the flag is later disabled. Shared JoinInvitation additionally prevents duplicate legacy/new invitation sends.
5. No automatic historical backfill. Existing paid stays keep their existing registry evidence. Explicitly selected reconciliation is separate. If paused/rolled back, retain new event ownership and reconcile pending/unknown references before any reassignment; never just restore both senders.

No production publication, owner approval, deployment, real delivery or end-to-end live guest test is implied by local test success.

## Preparation validation — 2026-11-08

- Broader MorningStay suite:275 passed,7 skipped (171.94s). Covers booking, real website payment hooks with fake providers, manual receipts, shared inputs/address, layout, old invitation behavior, fourth payment link, communications and gateways.
- Latest independent payment tests:43 passed,1 PostgreSQL-only skip. An additional contact-layout check passed under Python UTF-8 mode, matching the production/Linux text environment (44 total in that invocation). The full repository run was stopped before completion; it is not reported as passed. Its encountered Windows default text-encoding failure in the unchanged contact-layout export was verified to pass in UTF-8 mode without a product change.
- Seven skips in the broader run require PostgreSQL row locks. The new financial concurrency case is included in the prepared PostgreSQL CI list; CI has not been run because the branch is local and unpublished. Run that gate before approving production activation.
- Ruff lint and formatting: passed across528 Python files. Django system check and migration drift check: passed. Migration0030 has only been exercised in disposable local test databases.
- Full website phased journey: one Hostaway create, three core guest posts, one payment-link post, one checkout, one server payment verification, one independent success post, one invitation image, one booking event and one financial event. Failure/cancellation/pending/unknown/review create no financial notification event. Exact receipt filtering, wrong capabilities, duplicate sends, ambiguous provider outcomes, process-crash reference recovery, member/prior invitation suppression, safe pre-send deferral, rollback and legacy sender ownership are tested.
- This is website-side evidence. The separate project03 package owns its SQL/n8n integration report; the final combined review must include that report before publication.

On Windows use `PYTHONUTF8=1` for the normal pytest invocation. The JSON-lines bridge emits ASCII-safe JSON and does not require a production locale or credential.
