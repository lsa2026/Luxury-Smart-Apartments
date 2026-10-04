# HyperBill automated guest links — sandbox implementation, 2026-10-04

## Authoritative contract

Ahmad Qasem's business-email reply of 2026-10-04 supplies the sandbox base URL,
API login, Simple Invoice creation/retrieval, empty-body POST webhook returning
HTTP 200, Organization configuration, and production-account issuance after
sandbox acceptance. API fields were checked against the official downloadable
[API Blueprint](https://hyperbill.docs.apiary.io/api-description-document) and
[Simple Invoice documentation](https://hyperbill.docs.apiary.io/#reference/1/simple-invoice-collection/create-simple-invoice).

- POST `/api/login`: email/password -> `data.accessToken`.
- POST `/api/simpleInvoice`: final SAR amount, DB, name, email, international
  phone, language, merchant invoice number and expiration date.
- GET `/api/simpleInvoice/retrieve/{invoice_no}`: status and invoice identity.
- GET `/api/simpleInvoice/retrieve/min/{merchant_reference}`: recover uncertain
  creation without repeating a POST.
- States: pending, paid, canceled, declined. Obtain status from provider
  retrieval, not a browser return URL or webhook body.

## Operational path

Existing owner screen -> live quote -> owner final price -> existing Hostaway
unpaid reservation creation -> HyperBill invoice -> guest WhatsApp -> return to
reservations hub. Accounting/ Aseel messaging remains the default when disabled;
the modification/refund routes and existing card/Apple Pay flows are untouched.

One durable invoice per reservation; concurrent owner submits cannot create two
invoices. Creation timeouts require a GET reconciliation; never a second POST.
The same rule holds for ambiguous WhatsApp delivery.

Sandbox collections are stored separately in HyperBillInvoice; they cannot
create real PaymentAttempts, mark a live Hostaway reservation paid, initiate
refunds or fire production purchase analytics. Production rollout requires a
separate reviewed integration with issued production credentials and accounting
verification. Enabling a flag is not a production migration.

## Required UAT settings (no credentials belong in GitHub)

On checkout-uat-v2, not the main website:

| Key | Value |
| --- | --- |
| HYPERBILL_ENABLED | false initially; true after credentials and tests |
| HYPERBILL_BASE_URL | https://hyperbill-sandbox.hyperpay.com |
| HYPERBILL_EMAIL | sandbox API account email |
| HYPERBILL_PASSWORD | enter privately in Render |
| HYPERBILL_WEBHOOK_SECRET | random, at least 32 characters; private in Render |
| SITE_BASE_URL | https://checkout-uat-v2.onrender.com |
| HYPERPAY_ENVIRONMENT | test (existing UAT setting) |
| HYPERBILL_WHATSAPP_ALLOWED_NUMBERS | comma-separated owner-approved E.164 test numbers |
| ULTRAMSG_ENABLED / INSTANCE_ID / TOKEN | existing approved WhatsApp account |
| ACCOUNTING_WHATSAPP_NUMBER | existing client config requirement; not used for guest messages |
| HYPERBILL_RECONCILIATION_ENABLED | false for the dedicated DB-signal poller; disables general Celery dispatch |

Webhook: `https://checkout-uat-v2.onrender.com/payments/hyperbill/webhook/<private-secret>/`.
Enter only in HyperBill Organization webhook configuration. Keep the callback
URL out of public documents/screenshots and restrict access to request logs.
It accepts empty POST, coalesces wakeups and saves a durable signal.
The UAT start command `python manage.py run_hyperbill_uat` supervises the existing
Gunicorn web server and `run_hyperbill_worker` in the same paid service. The latter
checks durable DB signals every 10 seconds and recovers missed callbacks through
read-only reconciliation every 60 seconds. It never consumes general Celery queues,
creates invoices, sends messages or writes to Hostaway. Shared Redis provides the
existing reconciliation lock. Leave Celery dispatch disabled. Gunicorn access logs
omit request paths to avoid exposing the callback nonce. A child failure stops the
launcher so Render can restart the instance. This is an isolated UAT arrangement,
not the production-worker architecture. Without this poller or an isolated worker,
the signal is recorded but automatic verification is not operational.
Use owner-only admin actions or `python manage.py reconcile_hyperbill --reference
<merchant-reference>` for the first controlled test. The command performs only
status reads; it does not create a payment or send a message.

## Acceptance gate

1. Publish to the UAT branch only; check build, migrations and HTTP health.
2. Enter sandbox API credentials privately; verify API login succeeds.
   With HYPERBILL_ENABLED=true, build.sh performs a read-only API-login check
   and aborts the deployment on failure, preserving the previous healthy release.
   The owner booking list has a POST-only connection test: login only, no
   invoice, WhatsApp or booking creation.
3. Enable only UAT guest link mode; allowlist the owner's test phone.
4. Create/reuse one approved unpaid manual booking with the owner's final price.
5. Check invoice ID, amount, SAR, merchant reference and canonical sandbox link.
6. Verify UltraMsg accepted one guest message; guest confirms actual receipt.
7. Complete sandbox payment from the user's phone; verify paid via provider GET.
8. Configure/test an empty webhook POST, verify worker polling and queue recovery.
   Do not treat HTTP 200 as payment verification.
9. Confirm repeated actions cause no duplicate booking, invoice or message.
10. Report evidence to HyperPay only with separate email authorization, then
    await production-account issuance. Production and sandbox never share state.

No extra Render service or subscription is created by this implementation.

## Verification record

Local full-suite run: 1,409 passed, 1 PostgreSQL-only test skipped, 9 failed.
The same 9 failures were independently reproduced on the unchanged UAT base
commit 6c81f8bd: admin language-switch markup, missing refund-alert translations
(3 languages), CSS version assertion, unpaid-refund fixtures (3), and absolute
hero-image URL expectation. These are not fixed by this payment-link change.
Focused HyperBill/manual-booking/WhatsApp tests, lint and migration drift checks
must pass before UAT release. No provider call or real payment occurs in tests.
Owner actions are limited to 3 status reads or 2 first-time sends per request
to respect the existing web timeout. A missing WhatsApp configuration allows
safe setup and first delivery; an ambiguous network delivery never auto-retries.

Historical API check on 2026-10-04 initially returned a rejected login,
`hyperbill_login_credentials_rejected` (deployment dep-db195sdg1s2s739dn9n0).
After the user privately corrected the password and deployed
dep-db19guqd0e5s73eo22mg, the read-only command returned
`HYPERBILL_SANDBOX_API_CONNECTED (no invoice or message created)`.
HyperBill and UltraMsg remain disabled; successful authentication alone is not
invoice/WhatsApp acceptance. Final focused integration tests: 54 passed.

The user subsequently approved UAT Google owner-login setup on 2026-10-04.
The existing web-login OAuth client now includes the exact additional callback
`https://checkout-uat-v2.onrender.com/accounts/google/login/callback/`;
the existing production callbacks were preserved. Existing Google client
settings were copied privately to checkout-uat-v2 and Google sign-in enabled
(deployment dep-db19otugekts73crph00). No credentials are stored in this document.
The checkout UAT Blueprint preserves these three operator-managed settings
with `sync: false` and requires the owner-login preflight before migrations.
Owner-email enforcement stays enabled. The legacy UAT service and production
Blueprint remain unchanged. Owner sign-in was verified end-to-end: Google
returned to the UAT operations hub as the configured business owner, and
`check_owner_login` returned `Google owner login configuration is ready.`
The focused owner-login/HyperBill suite passed 44 tests; lint passed.
An approved manual-booking test remains pending; no Hostaway booking, invoice
or message was created by this setup.

### Subsequent live Sandbox verification, 2026-10-04 (Riyadh)

The earlier disabled-state/setup-only notes above are historical. With owner
approval, the original manual-booking test created Hostaway 67170248 once and
Sandbox invoice SI-INV-113974 for 10.00 SAR. The owner confirmed WhatsApp receipt;
the provider recorded Connector Test Mode success and manual API reconciliation
verified paid. This did not mark the real Hostaway booking paid.

The persistent isolated poller was deployed on checkout-uat-v2, release
`1d0fde7`, deployment `dep-db1b7d5ckfvc73dir9jg` (Live 23:16:12). A first launcher
deployment failed safely because it checked the unused REDIS_URL broker rather
than the configured shared CACHE_URL; the guard was corrected and the healthy
release confirmed HYPERBILL_UAT_WORKER_READY. No production service was changed.
63 focused tests passed; the corrected supervisor tests and HyperBill suite
passed all 46 tests, and lint passed.

New invoice SI-INV-113975, invoice_no `0d502ab18ab2fdd19c65f133f83483aa`, reference
`HBWH20261004A001`, 10.00 SAR, used a closed local `is_test` fixture without a
BookingIntent, property, Hostaway ID or Hostaway operation. It does not hold a
night or represent a real guest reservation. No new WhatsApp message was sent.
The provider's 3DS simulator approved its test card and displayed Connector Test
Mode success. The running poller changed pending to paid at 23:19:01.341585,
without a manual reconciliation command. The pre-existing global PaymentAttempt
count stayed 6; this test created none. Full invoice identity/money checks ran
before paid was persisted.

Controlled endpoint probes returned HTTP 200. Signal 1 was created 23:17:52 and
processed 23:18:00, while the invoice remained pending. Two duplicate probes after
payment coalesced into signal 2 at 23:20:01 and were processed 23:20:11. The paid
status and original verified_at remained unchanged. These were controlled probes,
NOT provider-originated webhook delivery. No additional provider signal arrived
after payment; the new automatic paid update came from the periodic GET fallback.

HyperBill Organization 1673 initially showed Webhook Disabled and a blank URL.
Active and the private callback were submitted, but the portal subsequently
required Google Authenticator verification, preventing a fresh read of saved
configuration. Persistence/provider delivery remains unverified. Ahmad Qasem was
emailed the precise distinction and asked to confirm organization/activation,
retry the notification, supply delivery evidence and production/refund guidance.
Confirmed sent Gmail message `1a10894a2aa7d4b1`, thread `1a0af5f2c1e5d9e6`.
This is Sandbox acceptance progress, not completed production readiness.
