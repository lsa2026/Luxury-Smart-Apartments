# HyperPay TEST/UAT and production integration

## Architecture and safety boundary

The integration uses HyperPay COPYandPAY with an explicit TEST or production
environment. Django
creates `/v1/checkouts` server-to-server, persists the checkout and merchant
transaction identifiers on the provider-neutral `PaymentAttempt`, and gives the
browser only the checkout ID through `paymentWidgets.js`. The access token is
never rendered, returned by an endpoint, or logged. Card number and CVV stay in
HyperPay's hosted form.

The return URL is not proof of payment. It triggers a server-to-server GET to
`/v1/checkouts/{checkoutId}/payment`, using the checkout ID already stored in the
database. Amount, SAR currency, DB payment type, merchant transaction ID, entity
ID when returned, and the payment-brand allowlist is checked before a payment
can become `succeeded`. Apple Pay is allowlisted by default in TEST only;
production keeps it hidden and unaccepted until an explicit go-live
configuration after HyperPay confirms the production entity.

After verified success, the existing local reservation preparation and
`HostawayBookingService` are used. The service revalidates live availability and
price before its one guarded Hostaway create call. Database row locks, the unique
payment identifiers, the one-to-one booking/reservation relation, and the unique
Hostaway create-operation ledger make refreshes and repeated result requests
idempotent. A lost-availability or uncertain Hostaway result does not invent an
automatic refund; staff must reconcile it.

Checkout creation also revalidates Hostaway immediately before contacting
HyperPay. This narrows the cross-channel race window. With the issued `DB`
payment type there is no inventory lock during card entry/3DS, so the post-payment
Hostaway revalidation remains mandatory; absolute charge-before-conflict
prevention would require a supported hold or an approved authorization/capture
(`PA`/`CP`) design.

Positive-price booking modifications use the same HyperPay ledger. The system
revalidates twice, verifies payment server-to-server, then updates and GET-reconciles
Hostaway automatically when the explicit modification flags are enabled. A zero
difference can execute automatically without payment. A negative difference is
not auto-applied until a verified refund workflow exists.

## Environment

Copy the placeholders from `.env.example` into the local secret `.env`:

```ini
HYPERPAY_ENABLED=false
HYPERPAY_ENVIRONMENT=test
HYPERPAY_BASE_URL=https://eu-test.oppwa.com/
HYPERPAY_ENTITY_ID=YOUR_TEST_ENTITY_ID
HYPERPAY_ACCESS_TOKEN=YOUR_TEST_ACCESS_TOKEN
HYPERPAY_CURRENCY=SAR
HYPERPAY_PAYMENT_TYPE=DB
HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED=true
```

Insert the issued TEST entity ID and access token, then set
`HYPERPAY_ENABLED=true`. Keep `.env` out of source control. Startup rejects an
environment/base URL mismatch, any host other than the approved TEST
`eu-test.oppwa.com` or production `eu-prod.oppwa.com` endpoint, a currency other
than SAR, DB changes, or missing credentials. Production requires separate
production credentials and pre-payment revalidation. TEST-only parameters are
omitted automatically in production.

For safe local startup:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py runserver
```

HyperPay TEST requires whole-SAR totals formatted as `xx.00`. A quote containing
non-zero fractional halalas is rejected before checkout instead of silently
changing the trusted Hostaway total.

## Checkout and widget

TEST checkout creation includes `testMode=EXTERNAL`,
`customParameters[3DS2_enrolled]=true`, `integrity=true`, customer names/email,
and the validated billing address. The widget script uses the returned SRI hash.
`wpwlOptions` is defined before the script with `paymentTarget: "_top"` for full
3-D Secure redirects. MADA is rendered in the first widget form and the native
HyperPay brand logo is used; VISA and MASTER follow in a second form.

CSP additions are route-scoped to `/payments/hyperpay/` and the single origin
`https://eu-test.oppwa.com` for scripts, connections, frames, images, fonts, and
form submission. COPYandPAY requires a runtime stylesheet, so `unsafe-inline`
is permitted for styles on this payment route only; it remains absent from
ordinary public pages. No `unsafe-eval`, global CSRF exemption, or site-wide
secure-header relaxation is introduced.

## Apple Pay TEST/UAT

Do not switch the production Render service from `production` to `test`. Run
Apple Pay on a separate UAT service with the test base URL and test entity/token,
an isolated database, and `HOSTAWAY_LIVE_BOOKING_ENABLED=false`. Keep all live
Hostaway write flags off in UAT. The checkout builder sends `testMode=EXTERNAL`
and `customParameters[3DS2_enrolled]=true` only when
`HYPERPAY_ENVIRONMENT=test`; it omits them in production.

The checkout page renders `APPLEPAY`, applies the native Apple Pay button CSS,
and sets the requested Saudi country/network options. A verified HyperPay
response with `paymentBrand=APPLEPAY` is accepted only after its stored checkout,
amount, currency, payment type, transaction ID, and entity are also verified
server-to-server.

Confirm HyperPay has loaded the payment-processing and merchant-identity
certificates into the TEST entity and enabled Apple Pay there. Because this
integration uses the merchant's own Apple certificates, register the site domain
in Apple Developer and host the exact domain-verification file downloaded from
Apple. (HyperPay says domain-file hosting is optional for UAT only when using
HyperPay's own Apple certificates.) Add that file to the UAT Render web service
as a Secret File named `apple_pay_domain_association`. The app serves its bytes
unchanged at the Apple Developer verification path:

`https://<uat-domain>/.well-known/apple-developer-merchantid-domain-association`

The `.txt` path is also served for compatibility with Apple's troubleshooting
documentation and older integrations.

Verify that this URL returns HTTP 200 and the exact Apple-provided body before
testing. Never fabricate the file or use production certificates in UAT. A
Sandbox tester account is for device-side payment testing; the Apple Developer
merchant ID and its certificates are separate configuration items.

The UAT acceptance test is: the sheet stays open, a sandbox payment succeeds,
HyperPay reports `APPLEPAY`, the server verifies it, the local test reservation
is created exactly once, and no real Hostaway reservation or live card charge
occurs. Capture provider result codes and internal IDs only; never PAN/CVV.

## HyperPay-provided TEST cards

Use these only in HyperPay TEST/UAT; never use real card details:

| Scenario | Card | Expiry | CVV |
|---|---:|---:|---:|
| VISA success | `4012000033330026` | `01/39` | `100` |
| Mastercard success | `5123450000000008` | `01/39` | `100` |
| Mastercard failure | `5204730000002514` | `01/39` | `251` |
| MADA test | `4464040000000007` | `11/26` | `850` |

## Manual UAT checklist

| Scenario | Expected payment | Booking | Hostaway | UI |
|---|---|---|---|---|
| Apple Pay TEST success | `succeeded` with `paymentBrand=APPLEPAY` | one local UAT reservation | no live writes | sheet remains open, then verified result |
| Visa success | `succeeded` | pending then confirmed | exactly one create after verification | confirmed, or operationally pending |
| Mastercard success | `succeeded` | pending then confirmed | exactly one create after verification | confirmed, or operationally pending |
| MADA success | `succeeded` | pending then confirmed | exactly one create after verification | confirmed, or operationally pending |
| Failed card | `failed` | unchanged | none | safe failure and retry option |
| 3-D Secure redirect | provider result mapped after server GET | never confirmed by redirect alone | only after verified success | top-level redirect and verified result |
| Refresh result repeatedly | unchanged success | one reservation | no duplicate create | same safe result |
| URL/resourcePath manipulation | unchanged | unchanged | none | 404 |
| Client amount modification | ignored | uses stored total | uses stored total | trusted total displayed |
| Provider timeout | prior status retained/no confirmation | unchanged | none | temporary verification error |
| Availability conflict after payment | `succeeded` | `create_failed` | no create POST | paid, booking pending manual review |

For each run, record the internal payment ID, merchant transaction ID, checkout
ID, result code, reservation status, Hostaway operation ID, and the visible UI.
Do not record PAN or CVV.

## Known limitations and production migration

- Apple Pay is implemented in checkout and server-side brand verification, but
  is not production-ready until HyperPay confirms the production entity and
  certificates and a separate UAT pass succeeds.
- TEST defaults to MADA, VISA, MASTER, and `APPLEPAY`; production defaults to
  MADA, VISA, and MASTER. Add `APPLEPAY` to the production
  `HYPERPAY_ALLOWED_BRANDS` setting only after the provider confirms readiness
  and the separate UAT pass succeeds. Do not enable `APPLEPAYTKN` unless HyperPay
  confirms acquirer-side token decryption and the widget is changed to request
  that brand.
- Payment type is DB. PA/CP and automated refund behavior are not implemented.
- TEST checkout rejects fractional SAR totals as required for this UAT setup.
- A verified charge followed by lost Hostaway availability requires manual
  operational reconciliation.
- The separate MADA guideline was supplied through an unencrypted, unverified
  QuickConnect URL. No script or asset from that URL is executed. Obtain the
  guideline through an official HTTPS HyperPay channel before Saudi Payments
  compliance sign-off.

## Credentialed TEST verification — 2026-08-30

- The issued TEST credentials were loaded only from the Git-ignored local
  `.env`; no credential was added to tracked files.
- A real server-to-server checkout was created successfully at
  `https://eu-test.oppwa.com/v1/checkouts` and returned `000.200.100`.
- The real checkout ID shape and returned SRI value pass the application's
  strict validation. The fetched `paymentWidgets.js` returned HTTP 200 as
  JavaScript and its SHA-384 digest matched the returned SRI exactly.
- The Merchant Area URL resolves over HTTPS to the HyperPay TEST login page.
- Chrome exposed a route-scoped CSP issue that prevented COPYandPAY from
  inserting its runtime stylesheet and submitting its hosted form. The CSP was
  corrected and regression-tested; ordinary public pages retain the stricter
  policy.
- The widget then rendered successfully with MADA first and its logo visible.
  A TEST Visa transaction completed the top-level 3-D Secure challenge and was
  verified server-to-server as `succeeded` with result code `000.100.112`.
- Hostaway creation remained safely blocked by
  `HOSTAWAY_LIVE_BOOKING_ENABLED=False`, leaving the verified payment in the
  operational-review state and making no Hostaway reservation write.
- A startup security check now rejects enabling live Hostaway booking writes
  while this TEST-only HyperPay integration is enabled, so a test-card payment
  cannot later be used to create a live Hostaway reservation.
- The published Marrakech unit was originally verified in MAD and correctly
  blocked from the SAR-only checkout. On 2026-08-30, listing `511786` was changed
  at the Hostaway source to SAR without changing its numeric base price (`1500`).
  A fresh two-night `priceDetails` quote then returned `3052 SAR`, and the local
  property currency was synchronized to SAR.

Before production: obtain the separate production entity/token and brand
approvals; select `HYPERPAY_ENVIRONMENT=production` with
`https://eu-prod.oppwa.com/`; enable the reviewed Hostaway write flags; confirm
live amount rules; re-verify CSP/widget SRI and MADA guidelines; complete
PCI/security review; test 3DS, reconciliation, monitoring, and incident runbooks;
then perform an explicit reviewed deployment. Refund automation remains a
release blocker for price-decreasing modifications and applicable cancellations.
