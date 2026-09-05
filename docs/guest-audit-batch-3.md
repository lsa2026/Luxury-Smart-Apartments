# Guest audit batch 3 — trust before payment

## Delivered

- Price pages now render every validated component returned by `priceDetails` and state that the authoritative total includes those displayed components.
- When the provider supplies only `totalPrice`, the guest sees that total, the presentation-only nightly average, and an explicit notice that tax and cleaning-fee inclusion was not itemised. No tax, fee, payable total, or availability value is derived locally.
- Guest-facing templates no longer name the internal channel manager. Operational and administration screens retain the provider name where staff need it.
- A locally managed approximate location is separate from the exact source coordinates. The public centre must be 100 metres or more from the exact point, the exact point must remain inside the selected 500–800 metre radius, and unsafe configurations render no map.
- Nearby-place names and distance descriptions are managed per property in Arabic, English, and French from the property editor.
- Properties with no public image render a branded LSA fallback. The property list in administration shows a public-image readiness indicator, and the editor displays a warning for a published property with no visible image.
- GitHub Actions now runs the same six-command quality gate for every pull request, because the repository previously had no CI workflow.

## Missing-image diagnosis

The audited Darat Safa card no longer reproduces as an empty card on the live site. On 5 September 2026 the browser received a public S3 source image with `complete: true`, `naturalWidth: 1350`, and `naturalHeight: 900`; the card exposed five public images. The earlier incident therefore reflects source/image visibility state that changed after the audit, not a persistent CSS hide in the current template. The new fallback and admin warning make that transient source failure safe and visible.

## Map privacy and CSP

The map uses OpenStreetMap's supported embed page. It does not call Nominatim, expose the source coordinates, place an exact marker, or add public JavaScript. The only new CSP source is:

```text
frame-src https://www.openstreetmap.org
```

It is appended only to a property response that has a validated public map. No wildcard, `unsafe-inline`, or `unsafe-eval` was added. Attribution remains visible inside the embed. OpenStreetMap documents the iframe export in its [export guide](https://wiki.openstreetmap.org/wiki/Export); its [tile usage policy](https://operations.osmfoundation.org/policies/tiles/) also makes clear that the community service is best-effort and requires visible attribution.

## Owner configuration after merge

For each property, the owner should:

1. Open the property in administration and expand **Approximate public location**.
2. Choose a neighbourhood centre that is not the property point, select a radius between 500 and 800 metres, then enable the map.
3. Add the approved nearby-place names and human-readable distance descriptions in the inline list.
4. Save. Validation refuses a centre within 100 metres of the exact source point or a circle that does not contain it.

No nearby-place claim or public map centre is seeded automatically; these are owner-managed presentation decisions.

## Visual evidence

- [Before — live location panel](audit-screenshots/p3/before-location-panel.jpg)
- [After — approximate map and nearby places](audit-screenshots/p3/after-approximate-location.jpg)
- [After — branded fallback asset rendered from this branch](audit-screenshots/p3/after-branded-image-fallback.jpg)
- Integrated local browser verification and the after screenshot confirm the 650-metre circle, privacy badge, attribution, three nearby places, and the branded no-image card in Arabic.

## Manual verification

1. Apply migrations and open a property with exact source coordinates in administration.
2. Set a displaced public centre, choose `650`, enable the map, and add three nearby places; save.
3. Open the public property page in Arabic. Confirm the map says it is approximate, displays a circle labelled 650 metres, keeps OpenStreetMap attribution visible, and never shows the exact address.
4. Inspect the response CSP. Confirm `frame-src 'self' https://www.openstreetmap.org` and `style-src 'self'`, with no new wildcard or `unsafe-eval`.
5. Hide all public images for a published test property. Confirm its listing and similar-stay cards show the LSA fallback and the administration list shows a failed public-image readiness indicator plus an editor warning.
6. Open a quote with components. Confirm each returned component appears and the inclusion notice is shown.
7. Open a quote whose stored validated breakdown is empty. Confirm the authoritative total, nightly average, and non-itemised tax/cleaning disclosure are shown.
8. Search guest-facing pages for the internal provider name; confirm it is absent while staff-only integration pages retain it.
