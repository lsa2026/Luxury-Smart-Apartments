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
   this Merchant ID and may affect UAT. Keep the prior certificate available;
   do not revoke or assume UAT remains unaffected.
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
