# Fixed catalogue links

Reference: SMA19-SHORT-CATALOGUE-20261119-001. This contract is a candidate until reviewed by19, deployed and verified live. Existing canonical property links remain valid.

For each language `ar`, `en`, `fr`:

- `/{language}/p/{code}/` returns302 to the property's current localized canonical detail URL followed by `#price-calendar`.
- `/{language}/p/{code}/photos/` returns302 to the property's current localized `gallery/` URL.

| Code | Stable Hostaway listing ID | Current canonical slug |
| --- | --- | --- |
| e12 | 315816 | luxury-smart-apartment-e12 |
| irqah | 315814 | spacious-and-modern-apartment-for-rent-in-riyadh |
| darat-safa | 315815 | darat-safa-luxury-apartment |
| a11 | 325961 | luxury-smart-apartment-a11 |
| b12 | 343666 | luxury-smart-apartment-b12 |
| nour-prestige | 511786 | luxury-smart-apartment-at-nour-prestige-marrakech |

The six identities match the existing property translation seed and published public-link contract. The lookup uses the unique listing ID and existing `Property.objects.public()` policy. Missing, hidden or inactive listings and unapproved codes return404. The villa is not included. Renaming a canonical slug does not change the short code. Codes are lowercase and the contract URLs include an explicit language prefix; a language cookie or query value cannot override that prefix. An unprefixed request still follows the site's existing LocaleMiddleware redirect; callers use only the explicit-language contract URLs.

GET and HEAD only. There is no URL-creation API, external target, query-based destination, database migration, redirect hit counter, provider request, price quote or booking/payment action. All query parameters are ignored and not forwarded, including dates, guest counts, tracking fields and `next`/`target`/`url`. Callers should use these exact unparameterized catalogue links; a separate tracking contract would need its own review. The302 response uses `Cache-Control: no-store` so visibility and canonical changes are checked again. `X-Robots-Tag: noindex, follow` keeps the destination canonical.

This prepares links only. Apartment descriptions, capacities, E12 bed notice and message layout remain owned by19 and are unchanged here. No guest message or production publication is authorized by preparation alone. Review code and tests in PR before release; after release verify all36 live short links (six properties × three languages × two destinations), the calendar fragment and gallery destinations before19 switches its live formatter.
