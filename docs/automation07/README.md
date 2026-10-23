# Scenario 07 — independent morning stays

Prepared workflow: https://lsa25.app.n8n.cloud/workflow/A9QK1p1P3e7fnpNU

Project: **Saeed Projects**. This is an unpublished, inactive draft with 38 nodes.
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
5. Preparation, dispatch and Contacts work are independently resumable on the minute tick.

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

## Prepared state and activation sequence

**No activation or live guest-message test has been performed.** The Supabase
settings row remains `enabled=false`, with zero reservation/job rows after rollback-only
contract tests. Website production still runs the restored release until the separate
07 website change is reviewed and deployed.

When activation is explicitly requested:

1. Deploy the website change and migration 0026 with `MORNING07_N8N_ENABLED=False`.
2. Verify the existing UltraMsg configuration without exposing secrets, the group's
   identity, the copied 07 apartment mappings/templates and all bound credentials.
3. Set only `lsa07_settings.enabled=true`; publish only this workflow. After its first
   published version exists, select its own ID as `errorWorkflow` so the dedicated
   error trigger handles this scenario's failures. n8n rejects selecting an unpublished
   workflow as an error handler, so this setting belongs to activation.
4. Set `MORNING07_N8N_ENABLED=True` and its exact dedicated webhook URL on the web,
   worker and scheduler services, then verify those deployments. Keep legacy
   `MORNING_N8N_ENABLED` disabled and its old webhook empty.
5. Perform an explicitly approved controlled real booking/receipt check; verify the
   single team notice, contact record, amendment/cancellation and actual checkout reminder.

The canvas has five stages plus three ungrouped trigger nodes (eight collapsed
boxes). This exceeds the SDK's seven-box readability recommendation by one. Merging
the booking ingress and scheduled preparation creates multiple group entry points
and is rejected by the SDK; the stage boundaries are intentionally preserved.

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
