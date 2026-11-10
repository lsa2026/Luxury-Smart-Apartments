# Hostaway guest classification — owner clarification 2026-11-10

Comparison authority is **Hostaway across all channels**, including Booking.com,
not local website history. The private list still displays its existing local
operational bookings, but another local record alone cannot establish returning
status. A valid normalized email OR phone matching a different confirmed Hostaway
reservation is sufficient. The current Hostaway reference never counts as another
booking. Cancelled/unconfirmed records and site-marked test references are excluded.
Names, account IDs and booking order do not establish identity. This is reporting,
not account linking, proof of a completed stay or marketing consent/audience export.

Official cursor/optional-filter reference checked 2026-11-10:
https://api.hostaway.com/documentation (Retrieve a reservations list). The API
documents `afterId`, terminal empty results and optional channel/date filters.
It also notes that records can move during live pagination. Repeated identities
are rejected rather than certified complete; completion records traversal time,
not a transactional snapshot of the provider database.

The existing vetted `HostawayClient.invoice_search_page()` boundary reads all
reservations in pages of 50 using `afterId`, with no channel/date/contact filters.
A finite Celery job processes one page at a time, with a one-second continuation
delay and no new periodic schedule. Launch is owner/manager-page triggered on
missing/stale evidence or explicit CSRF-protected refresh. Existing Redis cache
and worker are used; no new resource, credential, schema or environment is added.
No Hostaway writes, booking/payment actions or guest messages occur.

Only salted keyed contact digests, up to two distinct Hostaway references per
contact, technical cursors and timestamps enter private shared cache. Full provider
rows, raw email/phone, names and amounts are not persisted or logged. Web/worker
key-version mismatch, absent shared cache or transport failure cannot yield new.
No contact identifiers enter task arguments, browser polling or analytics.

New is permitted only from a complete, fresh scan ending in an empty page.
The current confirmed reservation must also appear in the provider scan before
claiming new, so empty/wrong-account or not-yet-refreshed evidence cannot certify it.
Repeated IDs/cursors, malformed rows/statuses, timeouts, missing job state and the
bounded 1000-page safety limit do not publish completion. Pending, failed, evicted,
incompatible or more-than-15-minute-old evidence yields unverified, never new.
Blank/invalid guest contacts yield insufficient data. The UI shows scan status
and actual completion time. A finite private status poll refreshes the page on
completion unless the user is editing search. Polling stops on failure, hidden
page or its bounded wait; a manual refresh is available. This is not monitoring.

Email uses validated trim/casefold without invented alias rules. Phone follows
the existing staff normalization for international/00 and Saudi national forms,
separators and Arabic digits. The current guest's contact identity and historical
comparison both come from Hostaway. Local rows provide only the current Hostaway
reference; stale website email/phone never override the provider's current identity.
Classification and segment filtering precede the existing 100-row display limit.

Synthetic tests cover a Booking.com-only prior reservation absent from the local
database, email-only/phone-only matches, own-reference exclusion, all-channel
pagination to terminal empty, partial/error/stale/cache-loss behavior, duplicate
tasks, malformed/repeated pages, privacy, filters, permissions and responsive
phone/tablet/desktop layouts. No development copy of production guest data is used.
Deploy through existing PR/main gates. Verify web/worker versions and source scan
status separately; publication does not prove the first live scan completed.
