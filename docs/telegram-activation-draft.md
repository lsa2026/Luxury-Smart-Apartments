# Booking-scoped Telegram activation — coordinated rollout

Owner approval in local thread39 now authorizes sequential deployment. Deploy
code/migrations with flags closed, then coordinate activation against the current
backend manifest. Do not create credentials or real bookings/payments/messages
for tests. Earlier draft-only restrictions and results remain historical.

## Website contract

The existing private guest management page and operations booking detail include a
booking-specific activation card. The guest action is POST
`/reservations/manage/<public_reference>/telegram/`, protected by the existing
booking-ownership guard and Django CSRF. The operations action uses the existing
booking detail POST with `action=telegram_activation` and its existing role checks;
no manager allowlist, grant, credential setup, or public lookup is added.

Opening a management page is not phone proof. Guest issuance requires the active
account assigned to the booking, an exact `VerifiedPhone` match, and primary
authentication within the existing 15-minute window, or an existing consumed
mobile-login challenge tied to the same session, user, phone and binding version
within that window. No login flow is modified and no OTP is sent. The binding is rechecked
before remote approval and before returning a capability. Administrative issuance
requires an explicit independent booking-specific phone attestation and internal
evidence reference. A Hostaway phone value alone is never sufficient. External
reservations with no BookingIntent can use the protected administrative action;
records that do not exist locally are not fabricated or opened through a public ID.
If the authoritative backend has no matching phone, issuance remains on hold.

`TelegramActivation` is a durable per-booking issuance guard, serialized using the
reservation row lock. Processing/uncertain outcomes block retries across workers
and restarts. An issued capability is returned only in its original response; its
URL/token is never stored in DB, sessions, cache, audit, redirects, or analytics.
An explicit request may replace an expired invitation, never a still-active one.
Each proof has an independent UUID and append-only audit entry; later attempts do
not erase the proof history. Telegram activation never creates VerifiedPhone or a
loyalty member and never changes booking/payment/terms data.

Response headers include no-store, no-referrer, noindex/nofollow and a restrictive
CSP that disables scripts on the one-time result. The Telegram destination and
token grammar are fixed; incoming query parameters cannot alter them. Scoped CSS
and Arabic/English/French copy reuse the site's existing base and brand palette.

## Configuration before any separately authorized activation

These defaults are deliberately closed:

```
TELEGRAM_ACTIVATION_ENABLED=false
TELEGRAM_LOYALTY_INVITATIONS_ENABLED=false
```

The website needs no service-role REST key or reusable outbound credential. Its
fixed bridge is https://lsa25.app.n8n.cloud/webhook/tg39-private-activation-v1.
It sends only contract, request_id, action and a signed capability valid for120s.
The bridge reads authority back from the fixed POST endpoint
`/integrations/telegram39/activation-authority/` using the existing Loyalty v1
credential held in n8n. The site retains only that credential's hash. No secret
is read/copied/shared/created, and the loyalty backup connection is not reused.

The adapter calls the bridge's health action first. HTTP200 alone is not readiness. Contract must equal
`tg20_guest_journey_v1`, ready must be true, and s01/reminders/reviews/contacts/loyalty
must all be true. Dispatch and cutover flags must both be true, matching the
backend's ready calculation including manifest verification. Activation therefore
remains closed until the package has an authorized cutover; this code never enables
dispatch. The backend functions, schema and manifest must be deployed and
verified by their owner before those checks can pass. Missing/malformed/false
health fails closed. No cloud DDL or grant changes are performed by this code.

Before approval/issuance, prepare refreshes and verifies the current backend
booking snapshot. Source readback validates booking ownership or an active
operations grant; writes additionally require the durable local phone proof.
Approve and issue have separate one-use consumed timestamps committed before
returning authority. A new proof/action UUID cannot bypass an uncertain result.
Capabilities are signed, not encrypted, and never persisted/logged: approved
phone claims travel only over HTTPS to the same trusted bridge authorized to
receive that phone. Only approve readback returns phone/evidence. Loyalty reads
can request health/binding under their existing active invitation credential,
never prepare/approve/issue. The bridge alone performs its private RPC/reads.
TLS is verified, redirects and environment
proxy inheritance are disabled, timeouts are bounded, and failures do not expose
provider bodies or automatically retry a possibly accepted POST.

The migrations are reservations/0036_telegramactivation (after deployed0035
WebsiteMessageRelay) and loyalty/0011_joininvitation_telegram_target_digest.
Follow the current rollout record for actual application state. Unknown
attempts require explicit reconciliation against backend evidence; this draft adds
no reset/retry bypass or administrator shortcut around that decision. Activation
UI currently covers ordinary Reservation only. MorningStay07 and DayUseStay08
require separate source/transport contracts and remain outside this UI scope.

## Loyalty

The existing invitation-only credential and shared JoinInvitation registry are
retained. Claim accepts the optional `channel=telegram`; old requests still default
to WhatsApp. Telegram claims and preflights verify the current booking-specific
private binding and pin a recipient digest. Membership and prior invitations
suppress duplicate invitations across both channels. Preflight is consumed once;
unknown outcomes cannot authorize another send. Telegram record accepts only
accepted (with a numeric message ID) or unknown, never delivered/read/WhatsApp
receipt lookup. n8n alone sends the photo/text after permission. This website never
posts to Telegram or WhatsApp.

Direct website stays remain owned by the website and blocked from the general
S01/L03 invitation endpoint. The reviewed normal nightly checkout uses existing
account/loyalty UI, not a generic post-payment invitation sender. The separately
implemented morning/day-use invitation senders and manual past-stay campaigns are
not converted or replayed by this draft. Full payloads and this ownership decision
are in project-history/01/telegram-activation-draft-20261122/LOYALTY-CONTRACT.md.

## Verification

Synthetic tests replace cloud calls and use the established isolated CI databases.
They cover phone proof, owner/guest roles, CSRF, external bookings, duplicate and
uncertain issuance, PostgreSQL concurrency, TTL and redirect validation, health,
private headers, shared membership/invitation suppression and Telegram outcomes.
Playwright renders static synthetic review pages at phone/tablet widths, aborting
all HTTP requests and never clicking Telegram links. No live end-to-end guest
activation, booking, payment or message is used to verify this draft. The handoff
records the final checked commit and CI results; do not infer readiness from a
draft PR or this document.
