# Website guest transport boundary — review only

Reference UM37-YCLOUD-SMA20-INTEGRATION-20261122-001. Website01 owns source
authorization/business checks, backend19 owns the single sender, automation20
owns scheduling, and37 owns provider/template approval. Telegram39 is unrelated
and untouched. Do not deploy this draft, send messages, create bookings/payments,
replay queues, or provision credentials from it.

## Implemented boundary

Eight existing call sites now prepare an immutable local route AFTER their
existing domain checks and BEFORE consuming their durable send claim:

| Source | Core messages | Verified payment | Payment link | Loyalty image |
|---|---|---|---|---|
|07|morning07_gateway|morning07_payments|morning_payment_messages|morning_loyalty|
|08|day_use08_gateway|day_use08_payments|day_use_payment_messages|day_use_loyalty|

The review setting WEBSITE_GUEST_MESSAGE_TRANSPORT defaults to ultramsg solely
to preserve the existing unpublished comparison behavior. No runtime setting is
read or changed. The added ycloud_central branch ALWAYS holds with
ycloud_contract_pending before making a new claim; invalid values and workgroup
recipients also hold. No endpoint, authentication value, YCloud provider client,
or fake HTTP adapter is introduced. Legacy client construction is deferred until
the original post-claim position. A prepared route refuses construction if the
selected transport changed. There is no central-to-UltraMsg fallback.

Core and payment receipt readers hold rather than query UltraMsg in central
mode. Accepted/sent are not invented as delivered; no central receipt is
fabricated from an old numeric ACK. No payment amount, invoice, booking,
financial proof, or invitation membership rule is changed. A held payment-link
delivery remains queued and no sweep/retry is scheduled. Source/domain failures
continue to be evaluated before the transport hold.

This is a **closed switch point, not a working integration**. It must not be
published as an enabled YCloud route. The existing default is not permission to
restart UltraMsg; actual runtime cutover requires the separately owned writer
fence and reviewed deployment. Rollback means hold/stop and retain records, never
selecting an old sender or replaying an old queue automatically.

## Verified source policy and gaps

Signed scoped capabilities, production environment, admission, current revision,
expected recipient, purpose allowlists, expiry/cancellation checks, immutable
reference/payload claims, and existing payment/receipt/loyalty guards remain in
their original owner.07 admission accepts verified booking OR verified payment;
08 requires verified booking. Do not collapse these distinct rules.

Current core send validates message type/length but NOT equivalence to an
approved template; job_key has a stay prefix, not a complete source-event proof.
Automation20 verified that event_id is absent from operational keys and revision
is absent from rules/guide/reminder keys. Immediate due_at is render time, not
original source occurrence. Payment keys carry event_id but need an authoritative
snapshot/proof/revision join. No timestamp is fabricated to admit a historical
event. Future reminders on older bookings require an explicit migration policy.

## Contract required before implementing the central branch

Read backend19 INTENT-CONTRACT.json, version yc19-intent-review-1. At this review,
ingest/status/events routes are null, caller authentication is not ready, and
currentDispatchAllowed=false. Its public provider webhook is NOT a website
ingestion endpoint. The YCloud API key/webhook secret must never enter the site.
The advertised credential variable is a requirement, not a created credential.

The contract requires source/event/booking/revision/opaque recipient/purpose/time
identity, booking and consent evidence, language, immutable content digest and
text-or-template payload. Do not fabricate missing evidence from an old
capability, caller consent=true, a phone value, the current time, or a type name.
No37 template approval or07/08 variable/media mapping is established yet; the
automation catalog contains proposals, not approved templates.

Before forwarding is possible, agree and test:

1. Concrete private routes, caller-scoped authentication, source resolution and
   actual backend revalidation immediately before a fenced provider POST.
2. Canonical07/08 event and revision join; stable request identity/content digest;
   backend-owned cutover and not_before/expiry without backfill or retimestamping.
3. Real purpose/language/template/media approval, current service-window and
   consent/opt-out evidence. Loyalty marketing consent remains separate.
4. Durable transport/request/source/purpose/recipient/digest pinning for every
   delivery and shared loyalty row, including crashes before result persistence.
   Do not infer a provider from the current setting or reinterpret old IDs.
5. Status results bound to the exact original request and provider/WAMID/evidence.
   queued/sending/accepted/sent cannot release delivered-only dependencies;
   delivered/read evidence cannot be downgraded by a late failure. On ambiguity,
   status lookup only with the same identity; no new POST or alternative ID.

Tests use disposable source fixtures and provider/network mocks, exercising
transport holds at all eight sites, unchanged source validation, no claim/ledger
consumption, no financial mutation, no fallback and no legacy receipt lookup.
The existing regression suites continue to verify duplicate/concurrent and
uncertain legacy claims. These tests do NOT verify a live central ingest or
provider delivery. The sole real owner test is managed by19, never this draft.
