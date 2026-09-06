# Guest audit — Batch 4

This batch improves browsing and property details without changing pricing or
availability ownership. Hostaway remains the source of truth for both, and the
new date filters do not call Hostaway while the catalogue or property page is
loading.

## Delivered

1. Added optional check-in and check-out filters to the property catalogue.
   Dates must be supplied together, cannot start in the past, must be ordered,
   and are limited to a 366-night browsing window.
2. Carried valid dates and the guest count into every property detail link. The
   booking form is prefilled there, but availability and the final price are
   checked only after the guest explicitly submits the form.
3. Replaced ambiguous RTL counters such as `5 / 1` with localized, readable
   counters such as `١ من ٥` in cards, the lightbox, and gallery pagination.
   The homepage `24/7` label is also localized and directionally isolated.
4. Kept the existing deliberate split between the visible property name and
   the SEO title, and added clear Arabic, English, and French admin guidance.
   Leaving an SEO title blank falls back to the visible property name.
5. Added meaningful alternative text to the remaining homepage content image.
   A regression test now rejects any image inside the homepage `<main>` region
   without meaningful alt text. Decorative logo images remain empty and are
   hidden from assistive technology by design.
6. Added an independent GitHub Actions quality workflow that runs the complete
   repository gate required for this batch.

## Visual evidence

- [Before: catalogue without date filters and ambiguous RTL counter](audit-screenshots/p4/before-property-filters.jpg)
- [After: desktop catalogue with dates and localized counters](audit-screenshots/p4/after-property-filters-desktop.jpg)
- [After: dates carried into the property booking form](audit-screenshots/p4/after-prefilled-property-detail.jpg)
- [Before: SEO fields without an editorial contract](audit-screenshots/p4/before-seo-admin-guidance.jpg)
- [After: explicit SEO title guidance in the admin](audit-screenshots/p4/after-seo-admin-guidance.jpg)

The responsive catalogue was also inspected at the browser's smallest allowed
viewport. It resolved to a single-column form with no internal horizontal
overflow, and the browser console reported no errors or warnings. A mobile
screenshot was not recorded because Chrome screenshot capture timed out; the
layout, semantic DOM, field values, and console state were still verified.

## Manual verification

1. Open `/ar/properties/` and confirm the arrival and departure fields appear
   with the existing filters.
2. Enter a future arrival date, a later departure date, and a guest count, then
   apply the filters.
3. Confirm the catalogue remains responsive and does not claim that the units
   are available or display a final price.
4. Open any property card and confirm both dates and the guest count are already
   filled in on the property booking form.
5. Confirm no availability or price result appears until the booking form is
   submitted.
6. Move through a property card and its full gallery. Confirm Arabic counters
   read `١ من ٥` (current image first), while English and French use their
   localized equivalents.
7. Open a property in the admin, expand an SEO section, and confirm the help text
   distinguishes the visible page name from the browser/search title and
   documents the blank-value fallback.
8. Open the Arabic homepage and inspect its main content images with browser
   accessibility tools. Confirm each has meaningful alternative text.

## Decisions and boundaries

- Catalogue dates improve continuity only; server-side availability filtering
  was intentionally not introduced because that would add Hostaway requests to
  page loading and require a separate rate-limit/product decision.
- No local price or availability value is derived or persisted by this batch.
- The separate visible and SEO titles are retained because the current data
  model already supports intentional editorial variation. The new admin copy
  makes that contract explicit instead of silently presenting a mismatch.
- No owner policy, price, legal copy, or operational setting is required for
  this batch.

## Quality gate

The branch is required to pass, in order:

```text
pytest
ruff check .
ruff format --check .
python manage.py check
python manage.py check --deploy --settings=config.settings.production
python manage.py makemigrations --check
```

The existing cache-version assertion was updated from `v34` to `v35` because
this batch intentionally changes the site stylesheet. The existing gallery test
that expected `1 / 5` was updated because that assertion documented the exact
RTL defect corrected by this batch.
