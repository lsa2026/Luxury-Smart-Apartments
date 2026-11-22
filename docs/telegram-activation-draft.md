# Telegram activation — review only, not enabled or published

Owner request: TG-JOURNEY-COMPLETE-20261122. Do not merge, deploy, migrate a live
database, create credentials, or send any messages while reviewing this draft.

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
TELEGRAM_ACTIVATION_SERVICE_KEY=
```

No suitable existing website service-role REST credential was found in the source
configuration. No secret was read, copied, shared or created. A later deployment
must determine whether an existing authorized server-only credential can be used;
this draft does not authorize adding privileges or creating a credential. The
loyalty backup database connection is purpose-specific and is not repurposed.

The adapter is pinned to the Apartments project `rsqgwepnowpnpealrzbn` and calls
`tg20_s01_health` first. HTTP 200 alone is not readiness. Contract must equal
`tg20_guest_journey_v1`, ready must be true, and s01/reminders/reviews/contacts/loyalty
must all be true. Dispatch/cutover flags must be booleans; activation itself never
enables dispatch. The backend functions, schema and manifest must be deployed and
verified by their owner before those checks can pass. Missing/malformed/false
health fails closed. No cloud DDL or grant changes are performed by this code.

Subsequent mutating RPCs are only `tg20_s01_approve_phone(p_key,p_phone,p_evidence_ref)`
and `tg20_s01_issue_invitation(p_key)`. Minimal service-role-only reads access the
existing binding and invitation tables for state and recipient verification; see
the full handoff BACKEND-REQUIREMENTS.md. TLS is verified, redirects and environment
proxy inheritance are disabled, timeouts are bounded, and failures do not expose
provider bodies or automatically retry a possibly accepted POST.

The two Django migrations in this draft are **not applied to production**:
reservations/0035_telegramactivation and loyalty/0011_joininvitation_telegram_target_digest.
They must precede any later code rollout, even when feature flags are off. Unknown
attempts require explicit reconciliation against backend evidence; this draft adds
no reset/retry bypass or administrator shortcut around that decision.

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
