# Nearby apartment discovery — v1 (2026-10-02)

## Scope and isolation

Opt-in Google Maps dialog on the six approved apartment detail pages only:
315814 (Irqah), 315815 (Darat Safa), 315816 (E12), 325961 (A11),
343666 (B12), 511786 (Nour Prestige Marrakech). The existing verified CID
allowlist is checked, but the centre is read from **that property's current
public_location_latitude/longitude**, not an airport destination or another unit.
No migration, import, reservation, payment or Hostaway operation is added.
The existing Leaflet map and both arrival-direction links are unchanged.

## Production settings (web service only)

| Setting | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_NEARBY_ENABLED` | `False` | Feature switch. False removes the trigger, assets and Google CSP exception. |
| `GOOGLE_MAPS_BROWSER_API_KEY` | blank | Separate browser key, HTTP-referrer-restricted to the HTTPS production hostnames. Never falls back to either server key. |
| `GOOGLE_NEARBY_MAP_ID` | `DEMO_MAP_ID` | Google Maps JavaScript Map ID for Advanced Markers. **Set the production Map ID before release**; the demo default is for development only. |

Use the existing `luxury-smart-apartments-web` Cloud project. Enable Maps
JavaScript API and Places UI Kit, and restrict the browser key to those services
and `https://luxurysmartapartments.com/*` / `https://www.luxurysmartapartments.com/*`.
Do not add localhost to the production key. A referrer-restricted browser key is
necessarily visible in page source; `GOOGLE_ROUTES_API_KEY` and
`GOOGLE_PLACES_API_KEY` remain private server-only keys.

The coordinating thread configures Google and Render, reviews/merges the PR,
deploys and verifies the real SDK. This implementation does not change those
services. Missing flag/key/location fails closed with no new Google request.

## Interaction and search

The locally served module/CSS load on eligible pages. Maps SDK, map and places
libraries load only after the guest opens the dialog. Opening itself creates
one map but performs **no Places query**. The same map is reused after closing
and reopening the dialog on that page. Closing before SDK load completes does
not create a hidden map. Escape, explicit close and backdrop click close the
native dialog and restore focus; the browser top layer prevents the WhatsApp
and booking buttons from covering it. No geolocation permission is requested.

Each category click connects one fully configured detached UI Kit request:

| Category | Included Google types | Initial radius |
| --- | --- | --- |
| Cafés | cafe, coffee_shop | 1 km |
| Restaurants | restaurant | 1 km |
| Laundries | laundry | 1 km |
| Supermarkets | supermarket, grocery_store, convenience_store | 1 km |
| Mosques | mosque | 1 km |
| Gyms | gym, fitness_center | 1 km |
| Bus stops | bus_stop, bus_station | 1 km |
| Metro stations | subway_station | 5 km |

Results use `DISTANCE` ranking (up to ten results), relative to the **search
centre**. Moving/zooming reveals Search this area; movement itself does not
change the request or fetch results. An explicit area search uses the map centre
and visible corner, with circular radius clamped to 100 m–10 km (20 km for
metro), not an exact viewport rectangle. Back to apartment recentres without
querying. Place selection displays ordinary `PlaceDetailsCompactElement` and a
place-ID Google Maps directions link. Locations/entrances are approximate;
straight-line distances are never presented as walking routes.

## Data, security and accessibility

Use only ordinary `PlaceSearchElement`, `PlaceNearbySearchRequestElement`, and
`PlaceDetailsCompactElement` / `PlaceDetailsPlaceRequestElement` (Essentials),
not Advanced/Pro UI Kit, `Place.searchNearby`, REST Nearby Search, `fetchFields`
or custom Places caching. Google results appear only with the Google map.
Search uses the built-in visible Google attribution; selected details include
`gmp-place-attribution`. Never remove or obscure map, photo or provider credits.
No Google lists, names, photos, ratings or details enter DB, Redis, logs or
browser persistent storage. Ephemeral result elements/markers are cleared on
close. The public apartment coordinates are already published on the site.

SDK/Places failures show a local explanation without changing any booking,
payment or arrival direction. Search has a 25-second timeout; lazy SDK has a
20-second timeout. Stale/closed responses are ignored. Google CSP domains are
added only to eligible detail pages, not checkout/admin/home. Google's current
SDK requires an eval/Blob-worker allowance on those pages; inspect the real
SDK console before release for blocked additional hosts instead of opening
wildcard HTTPS or all-site exceptions.

AR/EN/FR translations and compiled catalogs are included. Native dialog provides
focus containment and Escape. Buttons have minimum 44 px touch targets, logical
direction-neutral layout, visible focus and live statuses. The modal scrolls
on short screens and preserves Google credits. No human recommendations are
invented. Opening shares normal browser/network information with Google; the
modal must link to Google's Maps terms and privacy policy.

## Billing — verified against official docs, not a zero-cost guarantee

Sources:
[UI Kit overview](https://developers.google.com/maps/documentation/javascript/places-ui-kit/overview),
[Place Search](https://developers.google.com/maps/documentation/javascript/places-ui-kit/place-search),
[SKU details](https://developers.google.com/maps/billing-and-pricing/sku-details),
[global price list](https://developers.google.com/maps/billing-and-pricing/pricing).

Dynamic Maps bills successful map loads (`FAF4-3B2D-51B2`): currently 10,000
monthly free, then $7/1,000 in the first paid band. Places UI Kit Query
(`0678-4F72-DA7C`) bills **each Place Search or Place Details element request**,
regardless of displayed fields: currently 10,000 monthly free, then $1/1,000.
Search and selecting details can therefore be separate billable requests.
Map movement does not construct another map or automatically issue Places
searches in this implementation. Reopening a reused map does not construct
another Map instance. Reloading the page resets the reuse and local guard.

Example only: 1,000 visitors who open the map, make three category/area queries
and select two detail cards each imply about 1,000 map loads + 5,000 UI Kit
requests. Free allowances are shared across relevant project/SKU usage, so this
example is not a promised invoice. Ordinary UI Kit does not use the traditional
Nearby Search Pro tariff ($32/1,000 after 5,000 free) or UI Kit Pro tariff.

Local UI guard: one request at a time, at least one second between requests,
maximum thirty search/detail actions per page instance. **Not a spend cap**:
reloading, multiple clients and direct calls can bypass it. Use provider quotas,
restricted keys, billing monitoring and budget alerts for actual control. The
coordinator reports project daily limits of 300 Maps loads and 300 UI Kit Query
requests; verify effective quotas and actual billed SKUs after deployment.
Those quotas do not guarantee a zero monthly invoice or limit other SKUs.

## Release / rollback checklist

1. Run Python tests, Node interaction tests, Ruff, Django check and migration check.
2. Review coordinates/CIDs for all six, including Marrakech negative longitude.
3. Render flag/key/production map ID present on web only; no server credential in HTML.
4. At widths 320/390/430 test AR/EN/FR, keyboard, backdrop/Escape, focus restoration,
   no horizontal overflow, all buttons and unobscured attribution.
5. With the production key verify actual map tiles, all eight types, gmp-load/error,
   Places request count and console/CSP. **Mock local QA is not proof of real API success.**
6. Verify no SDK before opening, no queries while moving; area action and apartment return.
7. Check Google billing by SKU; record successful production map/search/details verification.
8. To roll back just set `GOOGLE_NEARBY_ENABLED=False` and redeploy web. Existing
   maps, directions, booking and payments remain available.

## Pre-merge local evidence

- Full Python suite: 1,662 passed; one PostgreSQL-only row-locking control skipped on SQLite.
- Nearby Node suite: 13 passed (lazy load/reuse, movement, radius/ranking, failures,
  timeouts, stale responses, selected details, throttling, focus restoration).
- Existing calendar, airport, address and guest-draft Node regressions also pass.
- Ruff lint/format, Django checks, and migration drift checks pass.
- Loopback mock preview: all AR/EN/FR × 320/390/430 layouts have equal client and
  scroll widths (288, 358, 398 px), and category targets are at least 44 px
  after device rounding. Native Escape restored the opener; reopening kept one
  map and did not add queries. Explicit category/area/detail steps counted
  individually; moving the mock map did not increment requests.
- Desktop 1100 px preview: no horizontal overflow. Preview helper is under
  `tests/`, not served by production, and is conspicuously labelled LOCAL MOCK.
- Real Google tiles, UI Kit internals, billing and production CSP **remain the
  coordinator's post-deployment validation**, not claimed by these local tests.
