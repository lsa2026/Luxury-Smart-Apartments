# Booking-list guest classification — 2026-11-10

Owner rule: either matching email **or** matching phone is sufficient to label a
guest returning. This is a private reporting segment, never proof of identity,
booking ownership, membership, completed physical stays or marketing consent.
No campaign, audience upload or export is initiated.

The rule applies across distinct confirmed bookings in the local site record:
regular reservations with a positive Hostaway reference, plus production morning
and day-use stays with confirmed Hostaway evidence. Cancelled/unconfirmed/test
records and payment attempts/drafts do not count. Multiple projections of the
same Hostaway booking count once. A guest with another matching booking is
returning on both cards, so the earliest card does not also enter the new-guest
filter. Dates, names and account IDs do not create contact matches.

Email is validated, trimmed and case-folded; plus tags/dots are not removed or
guessed as aliases. Phone uses the site's existing staff phone normalization,
supporting explicit country codes, international 00, Saudi national formats,
separators and Arabic digits. Blank/invalid contacts never match. With neither
usable contact, show insufficient data rather than claiming new. Results show
the match basis without exposing contact values. Contact matching never changes
guest access, account links or marketing consent.

Search and guest segment filters are applied after classification of the full
eligible local history and before the existing 100-result display limit. The
service uses three read-only streaming queries, not per-row lookups. It reads
only reference/contact columns and maintains per-request indexes, capped at two
distinct booking references per contact. Runtime/memory are linear in local
history size; no persistent contact cache or new synchronization job is added.
If history volume grows substantially, introduce indexed normalized projections
with a separate reviewed migration rather than silently truncating this scan.

Coverage is explicitly local. Guest account history projections without their
original contact identity and unimported external-channel history do not prove
new or returning status. Do not call Hostaway on page load for this feature or
claim complete external history. Existing operational status refresh is unchanged.

Verification uses synthetic rows, boundary/OR matching, source deduplication,
exclusion and query-budget tests, filtered results beyond 100 records, private
access checks and isolated phone/tablet/desktop layout checks. No production
guest data is read into the development environment. No migration, schema,
permissions, provider settings, financial flows or schedules change.
