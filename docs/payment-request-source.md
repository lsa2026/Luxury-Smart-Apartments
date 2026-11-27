# Ordinary payment message source

`apps.payments.payment_request_source` owns the existing `PaymentRequestDelivery`
link/receipt resources for ordinary website/admin bookings, approved amendments,
and verified extension children. It never collects, refunds, creates a payment
link, changes inventory, or posts to a messaging provider. Financial07/08 and SMA
invoices retain their separate sources.

The new lane starts closed: `MESSAGE_PAYMENT_REQUEST_SOURCE_V2_ENABLED=false`
and `MESSAGE_PAYMENT_REQUEST_SOURCE_CUTOVER_AT` empty. The existing relay,
source-auth, v2 and central transport controls are also required. Activation uses
one future time after web/worker/beat and the carrier/relay are verified ready;
the predeploy audit reports only counts, selected payment states and whether the
operations email matches the fixed business address. It performs no writes.

## Wire and identity

* Family `lsa01_payment_request`, scenario `01`, relay kind `payment_request`.
* The existing `site01-source-relay-2` envelope/header/URL carries only metadata.
* POST `/integrations/payments/requests/source-authorization/` with exact JSON
  `{job_sha256}` and `X-LSA-Payment-Request-Token` uses an event/job/binding-scoped
  signed READ valid for120seconds. No cookie, caller content, query or redirect.
* The delivery UUID is UUIDv5(request UUID,
  `payment-request-delivery:<decimal PK>:<kind>:<sequence>`).
  Event key is `website:01:payment_request:<delivery UUID>`; jobSHA is SHA256 UTF8
  of that event key plus `:<environment>`. Reservation key is the source-owned
  Hostaway ID; revision is the complete financial request fingerprint64.
* READ has `source_family=website`, `source_owner_family=website` and a required
  financial `template_context` with `source_family=lsa01_payment_request`.
  The carrier must explicitly normalize01; there is no07/08 or RPC fallback.
* `source_binding_sha256` is a top-level fact and context field. It binds original
  delivery/request/kind/sequence/environment, current booking/fingerprint/dates,
  amount/currency, approval/settlement, extension ownership/chain, recipient, URL
  hash, and the receipt's exact verified attempt/provider hash. No raw provider ID
  or phone is stored in the relay or expectation's financial binding.
* `payment_link/payment_link` selects `lsa_payment_request_v2` with ordered
  guest/reference/listing/amount/currency/arrival/departure/current URL.
  `payment_success/payment_confirmation` selects `lsa_payment_received_v2` with
  guest/received amount/currency/reference/listing. Both are BODY-only/ar,en,fr,
  independently selected from the pinned registry. An amended receipt describes
  the captured delta, without asserting payment of the entire booking.
* Source occurrence and not-before are the original delivery creation time;
  message expiry is24hours later. The existing payment URL remains state gated
  with no invented URL clock expiry. READ120seconds is separate from both.

## Claim, history, ownership

The private journal has one protected relation to the original delivery. A new
resource needs the dated cutover, pristine source/channel history and current
financial authority. Older deliveries retain their original channel. At/after
the boundary, a pause blocks dispatch without moving new deliveries to39 or
UltraMsg. A durable adoption also survives a pause/cleared setting. A previous
attempt, unknown result, workgroup copy or sibling sequence cannot be adopted.
An explicit repeat of a central request returns the original delivery.

The existing receipt endpoint accepts initial `{request_id,binding_ref}` only at
zero claim, retrieves authenticated proof/content/context and independently
rebuilds the source template under locks. It saves the immutable financial
binding in the same transaction as its one-to-one expectation. Projected
`receipt_expectation` includes its bindingSHA for carrier pre-POST verification.

Historical `{request_id,expectation_ref}` import uses that stored binding even
after pause, expiry, refund or booking change. Existing proof32 remains exact;
only signed delivered/read evidence establishes delivery. Accepted remains
accepted; failed/uncertain never become payment or guest delivery. Source01
failed receipts return `code=held` to the existing callback contract. Stronger
delivered/read is monotonic. Provider IDs longer than the original255-character
delivery contract are refused rather than truncated.

HTTP ambiguity is permanent uncertainty for dispatch. Recovery reads status
through the existing callback, never repeats provider POST or mints an alternate
delivery. Source hold/stale/uncertainty creates one redacted `integration_attention`
email on the existing operations mail queue. Admin group notices stay unchanged.
The Salman link manually sent by staff is excluded from new admission; there is
no historical scanner, backfill, guest/provider test, or financial side effect.
