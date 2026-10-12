# Apple Pay production rollout

## Evidence reviewed on 2026-10-12

- HyperPay/Dalia confirmed successful upload of both production certificates,
  and that Apple Pay uses the existing Visa/Mastercard/MADA production entity.
- Read-only verification on the running web service confirmed production,
  `https://eu-prod.oppwa.com/`, SAR/DB and the matching entity. No token was printed.
- Apple showed the production processing certificate MB2J6VGUC6 awaiting
  activation; the previously used certificate remained active. Do not revoke it.
- Only the two UAT domains were registered. The official hostname needs its
  own Apple-generated association file and verified registration.
- During activation, Apple explicitly warned that activating the new
  processing certificate revokes previous processing certificates for this
  Merchant ID. The owner completed activation after this warning was shown.
  MB2J6VGUC6 now appears as Active Certificate and the old processing
  certificate is no longer listed. Do not claim the old UAT can still work.
- The old production template combined APPLEPAY with MADA and used version 3;
  this preparation separates APPLEPAY and adopts the UAT-tested version 5.

## Release order

1. Deploy preparation with `HYPERPAY_APPLE_PAY_PRODUCTION_ENABLED=false`.
   Existing card forms, payment verification, amount/entity checks and booking
   ownership are retained. No experimental guest-data-free flow is migrated.
2. Register `luxurysmartapartments.com` under the existing Merchant ID. Save the
   exact downloaded association bytes as Render secret file
   `apple_pay_domain_association`. Do not commit it or any credential to Git.
3. Set `APPLE_PAY_DOMAIN_ASSOCIATION_FILE=/etc/secrets/apple_pay_domain_association`
   on the web service. Check both well-known URLs return 200 with exact bytes,
   over valid HTTPS, without sign-in, a language prefix or redirects.
4. Verify the official hostname in Apple. Verify every additional hostname
   actually used for payment separately; do not reuse a UAT-domain token.
5. Coordinate activation of MB2J6VGUC6 after the provider has loaded its matching
   key/certificate. Activation changes the certificate used for new tokens under
   this Merchant ID and revokes prior processing certificates according to
   Apple's confirmation dialog. Retain prior artifacts only for the audit;
   do not treat a retained file as an active certificate or assume UAT works.
6. Enable `HYPERPAY_APPLE_PAY_PRODUCTION_ENABLED=true` only after the above
   checks. This adds APPLEPAY to server verification and an independent widget
   panel; MADA, VISA and MASTER remain allowed.
7. Owner performs one real Safari/device payment with a real Wallet card on
   the official hostname. Verify the gateway success, amount/currency/entity,
   and confirmed Hostaway reservation, not just the Wallet animation.
8. If validation or the live test fails, disable the Apple Pay opt-in and
   reconcile any verified charge. Do not automatically repeat or refund a
   financial action without specific authorization.

## Acceptance

The rollout is not complete until a production Apple Pay charge is verified
server-side and its reservation is confirmed. A deployment, verified domain or
provider email alone is not that outcome.

## Primary references

- https://developer.apple.com/help/account/capabilities/configure-apple-pay-on-the-web
- https://developer.apple.com/documentation/applepayontheweb/configuring-your-environment
- Business email CRM:016237, message 1a12ee5ab014513d (2026-10-12).

## Desktop alongside mobile (2026-10-12)

The owner authorized enabling third-party desktop browsers on the official
hostname, without replacing the existing mobile checkout.

- HyperPay's official COPYandPAY Apple Pay guide explicitly supports third-party
  browsers with `checkAvailability: "applePayCapabilities"` and the configured
  `merchantIdentifier`. `buttonSource: "js"` avoids Safari-only button rendering.
- The public production widget inspected on this date supports this option and
  loads Apple SDK **1.3.2** with its own SRI. Apple requires SDK 1.2.0 or newer.
  Do not add a second independent Apple SDK/session or alter gateway hashes.
- The configured public Merchant ID is `merchant.com.luxurysmartapartments`.
  `HYPERPAY_APPLE_PAY_MERCHANT_IDENTIFIER` permits an explicit override if needed;
  it is NOT the HyperPay entity ID or access token.
- The wallet tab stays hidden until the gateway has completed its asynchronous
  capability check and rendered its actual button. Unsupported browsers, blocked
  SDKs and removed buttons leave normal card methods available.
- The Apple CDN is allowed in script/frame/image/connect/style directives only
  for rendered, enabled Apple Pay checkouts. Payment results, sign-in and public
  pages do not gain these permissions. CSP/SRI remain enforced.
- Desktop starts checkout and the customer scans Apple's dynamic code with an
  iPhone running iOS 18+ (or iPadOS 18+), then authorizes on that device. Mobile
  Safari continues to use the same gateway and server-side verification.

Release verification: run Python payment/refund/security regressions and the
JavaScript asynchronous availability tests; deploy and inspect the running
checkout configuration. A mock button or successful SDK capability check is
not a paid booking. Final acceptance still requires the owner's real desktop
and mobile payments, verified gateway success and Hostaway confirmation.
No additional charge, refund or cancellation should be triggered by diagnostics.

If rollback is needed, revert the desktop integration commit, preserving the
existing mobile Apple Pay production flag and all card methods. Turning that
flag off disables Apple Pay on BOTH desktop and mobile.

References:
- https://hyperpay.docs.oppwa.com/integrations/widget/apple-pay
- https://applepaypartners.apple.com/merchants/features/apple-pay-in-third-party-browsers
- https://developer.apple.com/documentation/applepayontheweb/loading-the-latest-version-of-apple-pay-js
