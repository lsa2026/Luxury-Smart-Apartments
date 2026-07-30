# Legacy site design audit

Reviewed source: `https://luxurysmartapartments.com/` on 2026-07-30.

## Preserve

- Brand name: Luxury Smart Apartments.
- Daily/monthly smart-apartment positioning.
- Riyadh and Marrakech as the current cities.
- Property catalogue, availability search, reviews, About, Contact, and booking management.
- Calm premium tone and direct path from discovery to date search.

## Improve

- True Arabic/English localization with RTL/LTR.
- Semantic heading hierarchy, keyboard navigation, focus states, and error summaries.
- Local PostgreSQL filters, responsive property cards, accessible gallery/lightbox, and clear quote steps.
- Privacy: city-level location only, no private address or Hostaway identifiers.
- SEO metadata, canonical URLs, structured data, and noindex on private booking pages.
- Mobile navigation and touch targets.

## Remove

- WordPress/Homey-specific login, registration, compare widgets, and duplicated navigation.
- Unverified contact details and conflicting brand/logo labels.
- Fixed pricing without stay dates.
- Heavy calendar and listing widgets that duplicate the Django/Hostaway flow.

## Redirect candidates for a later deployment phase

- `/properties/` → Django property list (same public path where possible).
- `/about-us/` → `/about/`.
- `/contact-us/` → `/contact/`.
- Legacy property permalink paths → the matching Django property slug.
- Legacy login/register/compare endpoints → an appropriate information page or HTTP 410.

No redirects are activated in this phase because the production URL map has not been approved.
