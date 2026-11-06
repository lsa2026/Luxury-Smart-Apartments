# Scenario 07 — independent morning stays

Published workflow: https://lsa25.app.n8n.cloud/workflow/A9QK1p1P3e7fnpNU

Project: **Saeed Projects**. The independent n8n workflow is published with 36 nodes and no Schedule Trigger.
The personal-project draft is now only a non-executable bookmark to this workflow;
it has one note and no credentials, webhooks, schedules or executable nodes.

## Scope and isolation

- Only website MorningStay bookings with confirmed, independently verified payment
  and a mandatory valid international phone number enter this journey.
- There is **no missing-phone recovery form or correction workflow**. Routine
  booking amendments/cancellation originate in the website's private operations UI.
- Confirmation, house rules, the property's access instructions, one work-group
  booking notice, and a checkout reminder use exact morning timestamps. No overnight
  dates, “tomorrow” checkout copy or default 12:00 checkout is inferred.
- Name and number enter the existing Google address book using a copied contact
  matching/merge implementation. Contact leases, completion and retries belong to 07.
  Its admission guard accepts 07's verified paid snapshot rather than legacy booking flags.
- No Execute Workflow node or legacy RPC/table is used for mutable runtime state.
  Every RPC is `lsa07_*`; the five tables are `lsa07_settings`,
  `lsa07_apartments_info`, `lsa07_message_templates`, `lsa07_reservations`, `lsa07_jobs`.
- Apartment configuration and house-rule/access templates were copied once into 07.
  Future maintenance of these copies does not change scenarios 01–06.
- Provider, database and Google accounts are shared infrastructure. Workflow
  execution and communication state are independent of scenarios 01–06.

## Website handoff

The isolated website modules are `morning07_communications`, `morning07_changes`
and `morning07_gateway`; the dedicated Celery task is
`apps.reservations.tasks.deliver_morning07_events`. It has its own minute schedule.
The bridge defaults **off** and is prepared for a separate website release.

1. Website commits a verified MorningStay revision and its transactional outbox event.
2. POST `/webhook/lsa07-morning-stay-v1` contains **only** `event_id` and its signed
   event capability. No guest, payment or recipient fields are trusted from the POST.
3. 07 GETs the fixed website `/integrations/morning07/events/<uuid>/` endpoint.
   An older event capability can return only the latest authoritative booking revision.
4. `lsa07_ingest` commits the independent reservation revision. Only then does 07
   acknowledge `{accepted:true,event_id:<the requested event UUID>}` to the website.
5. Preparation, Contacts and delivery are scoped to this stay and revision. The same execution uses an absolute Wait for its receipt window or checkout reminder, then checks the current revision before continuing. No idle polling executions are created.

Payment states open/processing/review do not confirm admission. No historical
backfill occurs simply by enabling the bridge. Cancellation preserves financial
and contact history; it does not perform a refund or write to Hostaway.

## Timing, amendments and receipts

- The checkout reminder is due at `ends_at - 60 minutes`, with `ends_at` as its
  expiry. If at most 60 minutes remain, checkout is already explicit in confirmation
  and access copy; no additional immediate checkout reminder is created.
- Every business revision supersedes obsolete jobs. Stable rules/access/reminder
  keys avoid resending completed identical messages. A metadata-only revision
  preserves the pending checkout job; changed checkout time creates a new one.
- Exactly one booking-status work-group job is keyed per revision. A separate
  technical attention notice is deduplicated across failed guest jobs in a revision.
- A provider POST is not proof of delivery. Exact provider ID/reference plus delivered
  or read receipt is required. An uncertain POST is reconciled; it is not blindly resent.
- The website's `Morning07DeliveryAttempt` has a durable unique reference and payload
  hash, preventing duplicate provider POSTs even during concurrent n8n retries.
- A stale worker cannot record a newer revision's receipt. Stale booking capabilities
  cannot send to a newly revised/cancelled booking.

## Credentials and access instructions

The bound draft explicitly uses the inspected **SAEED Apartment DB** Supabase
credential, **Google Contacts account** and **@LSA_Financial_Bot** in Saeed Projects.
Name-based n8n credential auto-assignment is ambiguous: verify bindings after
recreation. The shareable `workflow.json` deliberately omits credential bindings.
Provider secrets remain in the website's existing UltraMsg configuration, never in
07 Code nodes or JSON exports. The website gateway allowlists recipients and filters
receipt results to the exact 07 delivery attempt.

The property guide is reused, but its old promise about a morning portal sending
an apartment access code is replaced with support after identity verification.
This journey does not grant an automatic door-code bypass.

## Deployment state

The n8n workflow is published with no recurring trigger. The database switch is enabled. The `lsa07_event_*` workers are applied and tested in rollback-only SQL contracts. Website code is prepared with `MORNING07_N8N_ENABLED=False` by default; deployment and explicit web/worker environment configuration are still required before end-to-end operation can be claimed.

Configure only the web and worker services with `MORNING07_N8N_ENABLED=True` and the exact dedicated webhook URL after migration 0026 is deployed. No morning setting or task belongs to the scheduler. Keep legacy `MORNING_N8N_ENABLED` disabled.

A separate, two-node Morning07 error handler must be used; never point the scenario's error workflow at itself.

The final canvas has two stage groups with the website trigger outside them.

## Validation

- Native node-type and SDK validation; structural helper on the inactive export.
- Offline Node tests cover valid/invalid payment/phone, duplicate keys, metadata and
  same-day checkout amendments, cancellation, short stays, exact provider receipts
  and absence of calls to other workflows/legacy RPCs.
- `contract-check.sql` runs within a nested transaction that always rolls back its
  synthetic records and temporary enabled flag. It checks durable duplicates,
  invalid input, revision conflict/staleness, leases, uncertain POST reconciliation,
  metadata-only reminder preservation, amendments, contacts and cancellation.
- Django tests use synthetic bookings/provider mocks and cover payment proof,
  transactional outbox, signed capabilities, retry acknowledgement, provider POST
  idempotency, wrong recipients, stale revisions, cancellation and receipt filtering.
  A PostgreSQL-only test checks concurrent gateway POSTs.
- The full n8n workflow has not run in a disposable n8n runtime; no local n8n CLI is
  available. Native graph/schema checks and isolated code tests are distinguished
  from a live workflow execution. No live guest/contact/group writes were performed.

Native n8n tests with every external node pinned succeeded: invalid input execution 35886, valid booking 35887, duplicate event 35888. These tests prove native routing and Code-node execution with simulated services, not real guest delivery. Focused Django tests: 55 passed total, with 2 PostgreSQL-only concurrency tests deferred to CI. Node tests: 12 passed. Website release remains blocked before GitHub upload and Render configuration confirmation.

## Owner-approved payment notice — 6 November 2026

Website MorningStay creation now queues **one combined request confirmation and
payment-link WhatsApp message** using its existing UltraMsg account. It explicitly
states that final confirmation follows verified payment. It never carries access
instructions, codes or admission proof before payment. The owner can preview the
text and explicitly resend an eligible unpaid request from its detail page.

Resending rechecks the future exact property-local window, live Hostaway evidence,
local inflight reservations, other morning allocations and every unresolved or
successful payment attempt. Under the property/stay locks it renews the existing
link for five minutes, capped at arrival; it creates no Hostaway booking, price
change, financial attempt or new payment URL. The worker rechecks eligibility and
claims the durable send intent before a provider POST. Replaying a task or form
nonce does not resend; a fresh deliberate click can. Ambiguous provider outcomes
are displayed for review rather than automatically reposted. Historical records
are not scanned or sent during deployment.

The published workflow `A9QK1p1P3e7fnpNU` was inspected through the connected n8n
source. Its paid-only renderer creates house_rules and checkin_guide with due_at
set to the current event time. Existing ordering and receipt verification remain.
No workflow, schedule, shared 01–06 configuration or financial verification was
changed. Supabase `lsa07_settings.enabled` was independently read as true.
The copied AR/EN/FR templates in `lsa07_message_templates` were read directly.
The Arabic guide contained an obsolete promise of automatic code delivery via an
absent portal link. Only that exact phrase in 07's Arabic checkin_guide was
replaced with contacting support after identity verification, consistent with
07's existing English/French renderer. No real access code or apartment data
was read or copied. Rollback of this template-only correction is the inverse
replacement of the two literal sentences; the 01–06 templates are unchanged.

Release rollback: redeploy the previous website/worker commit, leave the additive
0027 delivery table intact for audit, and do not replay queued or uncertain sends.
No scheduled job was added. Tests use synthetic guests and mocked providers only.

### Unfinished existing stays — owner clarification, 6 November 2026

Explicit manager resends can now renew an unpaid existing stay after its original
arrival time, provided the actual checkout has not passed. Live availability,
other allocations, payment-state and recipient checks still apply. The five-minute
renewal is capped at checkout. Unrenewed expired links remain closed; new booking
creation still requires a future arrival. Arrival, checkout and the approved price
are retained. The payment page and checkout revalidate the started window, and a
verified payment while it remains unfinished confirms admission and triggers the
existing instructions event. A payment received after checkout remains recorded
for review. No historical send, payment or guest action is performed by deployment.
