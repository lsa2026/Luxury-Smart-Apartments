# Optional payment-address search (Google Places API New)

This is an enhancement to the existing shared guest checkout, not a new booking
or payment workflow. The guest chooses an address, reviews/completes its fields,
then submits the same GuestDetailsForm as before. No reservations or payment
records are created by these endpoints.

## Activation on the production web service

- `GOOGLE_PLACES_ENABLED=true` enables the optional search.
- `GOOGLE_PLACES_API_KEY` is server-only. If unset or empty, it uses the existing
  `GOOGLE_ROUTES_API_KEY`. That key must permit **Places API (New)** as well as
  Routes, and its existing Render egress IP restrictions must remain in place.
  Do not put the key in HTML, browser JavaScript, Git, a URL or this document.
- `GOOGLE_PLACES_DAILY_LIMIT=300` is the combined Autocomplete + Details request
  allowance, across all web processes, counted in Redis before provider calls.
  UTC calendar days are used. Failed provider calls also consume the allowance.
- `GOOGLE_PLACES_REQUIRE_SHARED_CACHE=true` is the safe production default.
  A non-Redis backend or unavailable Redis disables search, not checkout.

Only the web service needs the feature flag. No new service, worker schedule,
database migration, browser SDK or CSP permission is necessary. Configuration
is deliberately opt-in: an ordinary code deployment does not activate paid
calls. Set the flag false to turn off search without changing payment.

The 300/day ceiling is a conservative safeguard, **not a guarantee of a free
Google bill**. Pricing, session billing and other callers on the same project
must be considered. Google Cloud quotas/budget alerts are complementary. Routes
has a separate counter and its existing configuration is unchanged.

## Requests and cost/privacy protection

- Two same-origin, CSRF-protected POST endpoints under a signed quote URL:
  `address-suggestions/` accepts exactly `{ticket,input}`;
  `address-details/` accepts exactly `{ticket,place_id}`.
- They require the unexpired, HMAC-valid, active quote and its owning browser
  session. Tickets are additionally quote/session-bound and last 10 minutes.
- A new Google UUID4 session token links Autocomplete to one Details call.
  Consuming Details rotates the signed token even after a provider failure;
  replays cannot execute a second Details request. Expiry renews without a paid
  call. Only place IDs previously returned in that session can request Details.
- Fixed Google endpoints and field masks: Autocomplete uses only prediction
  IDs/text; Details uses **`addressComponents,formattedAddress`** (Essentials),
  never `*`, `displayName`, reviews or Pro fields. No arbitrary field/proxy input.
- Worldwide neutral location bias; no Saudi/Moroccan country restriction.
- Frontend debounces 600ms and starts at 3 characters. It cancels obsolete
  requests and ignores late responses. No automatic retry of provider failures.
- Additional fixed bounds: 25 calls/browser-session/10min, 40/quote/hour,
  60/IP/10min, 20/Google search-session/10min and 60 globally/minute. Lower-level
  rejection short-circuits before reserving daily usage. Shared IPs can hit the
  IP cap; the guest retains the complete manual checkout.
- Redis stores only bounded counters and short-lived token/place-ID digests.
  No typed address, formatted address or suggestion list is stored or logged.
  Responses are `no-store` and `noindex`. Provider errors are generic, without
  response bodies, credentials or address text.
- The search input has no form name and cannot be stored by the existing draft
  collector. This feature emits no analytics events or raw address parameters.
  Selected billing fields remain part of the existing guest-approved booking
  record/draft; the existing city/country analytics allowlist is unchanged.
- Google Maps attribution appears with suggestions. Existing privacy/terms
  pages explain the optional data sharing and link Google terms/privacy.

## Address mapping and fallback

Country is an accepted ISO alpha-2 option. Locality/postal town maps to city,
administrative area level 1 (provider subdivision abbreviation when available)
to state/region, and street number + route to street. Suggestions use the UI
language; Details components use English to obtain representable payment-region
names/codes without inventing translations. Unrepresentable/missing regions
are not autofilled; the guest retains manual entry and review.
US postal suffixes are included as ZIP+4. Valid postcode spaces/hyphens are
preserved for UK, Canada, Japan and other countries in the form and booking
record. At the HyperPay boundary only, postcode spaces/hyphens are removed to
meet the provider's documented AN16 format. State separators/Latin diacritics
are compacted for AN50 while preserving the original record. Non-Latin legacy
manual regions are never erased/transformed into guessed codes; their existing
behavior is unchanged. The 16-character postcode storage limit is unchanged.

Missing or overlong components are omitted, not invented or truncated. Manual
values are retained. Changes made while Details is pending take priority over
the response. Input/change events keep form validity/draft handling consistent.
Guests must review and complete required fields: Places autocomplete is **not
address validation**, and does not guarantee bank/HyperPay payment acceptance.

## Verification

Run `pytest tests/test_checkout_address_places.py tests/test_billing_address_minimum.py`
and `node --test tests/checkout-address.test.cjs`. All external calls are mocked;
tests never use real credentials, create reservations or charge/refund cards.
Then run the complete test suite and the ordinary production build checks.

After deployment, use a fresh unexpired quote to select a public sample address
without submitting a booking. Verify attribution, ISO country, address fields,
postal spacing, manual correction, keyboard/touch operation, and absence of
horizontal overflow at 320/390/430px in Arabic, English and French. Verify manual
entry still works if the service is disabled or limited. This requires no payment.

## Official references

- [Autocomplete New](https://developers.google.com/maps/documentation/places/web-service/place-autocomplete)
- [Details fields](https://developers.google.com/maps/documentation/places/web-service/place-details)
- [Session tokens](https://developers.google.com/maps/documentation/places/web-service/place-session-tokens)
- [Attribution and end-user-address exception](https://developers.google.com/maps/documentation/places/web-service/policies)
- [HyperPay billing formats (AN16 postcode; AN50 state)](https://wordpresshyperpay.docs.oppwa.com/reference/parameters)
