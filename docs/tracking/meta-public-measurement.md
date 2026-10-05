# Meta public measurement through the existing GTM container

Container: `GTM-M9VLL75K`. Pixel: `904593259205579`.

The first-party bridge runs only on the existing public localized route allowlist.
It requires enabled GTM, enabled cookie consent and a fresh marketing choice that
sets `metaMeasurement: true`. Older Google marketing choices and the independent
Google email-matching opt-in do not grant Meta permission.

Import `lsa-meta-public.tpl` as a private tag template. Do not publish it to the
Community Template Gallery. Create one tag, `LSA — Meta — Public measurement`, with
additional consent requirements `ad_storage` and `ad_user_data`. Use these triggers:

- Initialization — All Pages.
- Custom Event, regex `^(lsa_meta_consent|view_item)$`, all custom events.

The template can execute only `LSAMeta.track` and read only `event`, `property_id`,
`property_name`, `page_language`. It cannot read forms, identity fields or arbitrary
window variables. The bridge is responsible for the stricter Meta choice and
public URL checks. Keep automatic advanced matching and automatic event detection
disabled in Meta. The bridge disables automatic pixel configuration and does not
pass advanced-matching data to `init`.

## Events and limits

- `gtm.init` or `lsa_meta_consent`: one `PageView` per page after approval.
- Existing `view_item`: one `ViewContent` per property on that page, with only
  public property identifier/name and optional page language. The existing Google
  journey emitter requires analytics consent, so marketing-only consent measures
  PageView, not ViewContent.
- Purchase, checkout, contact, UPD and other events are deliberately ignored.

Do not add Meta to payment or private booking pages. HyperPay result URLs contain
a private `return_token`. Public pages reached with a private referrer, URL hash,
unknown query keys or suspicious attribution values are conservatively excluded.
The original URL is never rewritten to bypass these exclusions.

Do not modify the Google purchase trigger, GA4/Ads conversion tags, UPD accessor,
purchase acknowledgment or transaction deduplication. Loading GTM for a verified
Google purchase cannot load Meta on an excluded page.

SDK failures affect only Meta. Withdrawing consent clears queued events and revokes
future SDK collection. It does not promise to erase past data at Meta. No denied
events are replayed on a later approval.

## Deployment and verification

Deploy the website bridge, translated cookie descriptions and privacy/cookie
notices first. Keep the GTM change unpublished until the website is live, then
preview the new tag. Verify rejection, old consent and private-page exclusion,
then grant a fresh marketing choice and verify public PageView/ViewContent in Meta
Test Events. Publish only the added template, trigger and tag. Preserve all six
existing Google tags and their triggers.

Run the Node consent/Meta/UPD/purchase regression suite and the Django Meta,
marketing, UPD and HyperPay tests. CI runs the full quality gate. Do not generate a
fake live purchase or replay an old reservation to test Meta. Meta purchase/CAPI
needs a separately reviewed server-side design that excludes private URLs and
identity data without a specific opt-in.
