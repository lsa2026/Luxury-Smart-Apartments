# Guest lifecycle audit and owner-only membership closure

Owner request: 17 October 2026. Closing means loyalty membership only, never deletion
of the website account, reservations, payments, original referral history or ledger.
No public self-service withdrawal action is introduced. Joining remains self-service.

## Owner interface

`/admin/loyalty/memberships/`, linked from loyalty administration. Existing owner
authorization is reused without granting roles or changing permissions. Arabic RTL,
responsive cards, chronological 25-row pages, event filtering, search by current or
historical mobile/email or membership number. Pre-membership guests are included.

Closure requires selecting a guest, opening the disclosure, entering a reason/request
reference and confirming both identity/request verification and point forfeiture.
A ten-minute signed state binds the member, balance, held points and update time.
Execution rechecks under the existing financial transaction lock. GET does not mutate;
CSRF applies. A changed/replayed/cross-account review cannot cancel another member.

The existing closure policy is retained: outstanding stays and holds must be settled
first; a positive balance becomes one linked ledger debit; zero requires no fictitious
posting; negative balance survives. Recipient gifts are untouched. No automatic card
charge, restoration/rejoining policy or new general termination clause is introduced.

## Evidence

New append-only `loyalty.GuestEvent`: customer, nullable member/actor/ledger references,
kind, unique event reference, source, reason, before/after values, event time and separate
recording time. Database triggers reject updates/deletes and PostgreSQL truncation.
Private owner interface only; no generic CRUD or guest endpoint. No OTPs, passwords,
tokens, provider request/response bodies or message text are stored here.

Events: account creation, email proof, verified mobile binding, voluntary loyalty join,
administrative contact identity changes, administrative referral revisions (including
affected descendants), guest-saved profile changes, membership closure. Points remain
solely in the existing financial ledger; closure links to its debit when applicable.

Migration 0006 reconstructs only provable old account creation/member join/withdrawal,
current verification timestamps, recorded contact changes and referral revisions.
Recovered records say `history`; missing original operator/contact details are not
invented. Previously unrecorded profile changes cannot be reconstructed. Existing
members, consents, ledger rows, rates and relationships are not rewritten.

The existing external snapshot includes the event records as context, including
pre-enrollment registration/proof evidence, with their necessary account references.
Unrelated pre-member bookings, sessions and social credentials remain excluded.
New histories are checked against disappearance/rewrite. Previous snapshot schemas
remain accepted without forging historical events or rewriting original seals.
No external DDL/grants, destination/secret/schedule change or manual backup is needed.

## Verification

Focused offline tests cover lifecycle/verification/identity events; positive/zero/negative
closure; recipient/history preservation; booking guard; required confirmations; CSRF,
guest/staff denial; signed-review tampering; search/filter/pagination; immutable ORM/SQL
history; historical backfill and snapshot compatibility. Full suite and isolated
PostgreSQL CI are release gates. A fictional read-only in-memory preview checks mobile
layout. Production validation is read-only: no real guest closure or points movement.

The project follow-up issue retains actual CI, merge and deploy evidence and owner
acceptance status. Shipping code is not proof of the next daily backup execution.
