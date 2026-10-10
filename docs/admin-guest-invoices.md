# Owner guest accommodation invoices

Entry: `/admin/operations/invoices/`, linked from the booking administration hub.

- Owner-only, CSRF-protected, no-cache. Guest PDFs have no public URL.
- A numeric Hostaway ID performs one on-demand, read-only reservation lookup.
- Lookup also accepts a guest phone with Saudi Arabia as the default country and
  the same libphonenumber parser as checkout: national starting zeros, Arabic digits,
  spaces, `00`, and a full international number. Invalid/ambiguous input is rejected.
- Hostaway documents `match` for guest names, not a phone filter. Read-only phone
  search therefore compares full normalized numbers in memory over at most two
  100-row cursor pages per click (one GET attempt, eight-second timeout per page).
  An explicit continuation button searches older pages; incomplete searches never
  claim no bookings exist. No guest phone is added to a provider URL, audit metadata,
  public link, new cache or stored search-history table.
- Matching stays display apartment, guest, platform reference and dates; the owner
  must select a stay even when there is only one result. Search alone creates no
  invoice or reservation. Expiring signed POST tokens bind selection/cursor to the
  owner and phone; fresh lookup rechecks the phone before preparing the invoice.
- Stored international numbers are normalized; national numbers require a verified
  two-letter `guestCountry`. Missing/masked/ambiguous contacts need lookup by booking
  ID instead. Booking search stays owner-only, CSRF-protected and private/no-store.
- No external booking is imported into `Reservation` or the administration booking list.
- Snapshot contains only guest/contact, platform reference, apartment, dates, nights,
  total, currency, supplier and retrieval timestamp. Card/access-code/raw payload data
  is excluded. No payment state is printed or used to infer a payment.
- Airbnb `confirmationCode` is preferred. Otherwise `channelReservationId` is used;
  missing/internal identifiers require the owner's guest-facing reference before issue.
- The total is Hostaway's `totalPrice`, not platform payout/net proceeds. The owner
  must review the accommodation amount. No VAT, tax number or compliance QR is invented.
- External channel currency is preserved. Only channel 2000 direct bookings use the
  property's existing documented currency-label override; no new FX calculation.
- A refreshed snapshot invalidates an earlier review, preventing stale-amount issuance.
- Issuance is row-locked and unique per Hostaway reservation. PDF, language, number,
  timestamp and reviewed snapshot are frozen. Double clicks return the existing PDF.
- The PDF embeds licensed Amiri fonts and the approved Figma LL light logo, with
  Arabic, English and French copy. Font/license assets ship with the application.
- Email uses the existing backend; WhatsApp uploads the PDF privately as base64 to
  UltraMsg. Neither adds a paid service, public storage bucket, nor periodic task.
- Sending requires a separate owner-confirmed recipient request. Unique invoice /
  channel / normalized recipient prevents duplicates. A provider acceptance is NOT
  read/delivery confirmation. Timeouts and ambiguous outcomes require manual review;
  they are never retried automatically. Explicit rejections may be retried by an
  additional owner click after fixing the connection; attempt count is retained.
- Queue runs on the existing `lsa-worker`. If the worker stops, send requests stay
  queued; PDF download remains available. Queue acknowledgement errors surface as review.

This is an accommodation document, not a claim of certified statutory e-invoicing.
Where a compliant tax invoice is required, issue it through the appropriate process.

## Verification and rollout

`pytest tests/test_guest_invoices.py` covers isolation, references, currency, amount
validation, three-language PDFs, private access, immutable issuance, stale reviews,
explicit sends, provider failures and idempotence. PostgreSQL CI verifies row locking.
`scripts/preview_guest_invoice.py OUTPUT_DIR` creates synthetic previews only.

Deploy web and worker from the same approved main commit; migration adds two independent
tables, without rewriting bookings/payments. Verify both services and the owner page.
Do not issue or send a real guest invoice merely to test deployment. Rollback application
code if needed while retaining the new invoice tables and issued documents.
