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
ID when returned, and the MADA/VISA/MASTER brand allowlist are checked before a
payment can become `succeeded`.

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

- Apple Pay is not implemented; it is waiting for HyperPay configuration.
- Only MADA, VISA, and MASTER are enabled.
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
