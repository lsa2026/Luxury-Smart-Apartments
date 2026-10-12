# Consent-gated payment journey — 2026-10-12

Scope: all public apartments, Arabic/English/French. Observation only; do not
change the provider checkout, Apple authorization, card validation, amount,
purchase confirmation, receipt acknowledgement, UPD or Ads conversion action.

## Event meanings

1. Existing begin_checkout: guest submits the booking details.
2. Existing booking_intent_created: booking review is rendered (not a sale).
3. payment_checkout_requested: guest submits Continue to secure payment.
4. secure_payment_view: merchant secure checkout is rendered.
5. payment_widget_ready: COPYandPAY reports its forms ready, once per page.
6. payment_method_selected: deliberate mouse/keyboard method change. A default
   mada selection or programmatic Apple availability fallback is not a choice.
7. payment_submit_clicked: trusted activation of a visible provider pay button.
   This is intent only: it does not prove submission, authorization or payment.
8. add_payment_info: COPYandPAY onAfterSubmit reports payment submission.
   The documented callback applies to card forms; Apple sheet/device stages
   must not be assumed covered. Verify its actual behavior in the owner's test.
9. payment_verification_started: gateway reports its 3DS iframe loaded, not a
   completed verification. No iframe contents, OTP or bank URL are collected.
10. payment_widget_error: only allowlisted error categories, never messages.
11. payment_result: latest server-verified attempt state on the result page.
    Render/refresh counts are not distinct transactions. A result of succeeded
    is a verified charge, NOT necessarily a confirmed apartment booking.
12. Existing purchase: still emitted only after verified payment AND confirmed
    Hostaway booking, with unchanged transaction and receipt deduplication.

The browser emits journey events only with analytics consent. It never replays
pre-consent clicks or errors. If consent is granted on the current checkout,
the current view/readiness can be reported. Existing anonymous purchase
behavior is unchanged. Consent is not a guarantee of receipt: ad blockers,
network failures or leaving before return can still prevent browser telemetry.

## Method versus brand

payment_type: card, apple_pay, unknown.
payment_brand: mada, visa, mastercard, apple_pay, unknown.
Pre-payment card tab has unknown brand until the verified gateway reports it.
Purchase metadata uses PaymentAttempt.payment_brand, never the selected tab,
local storage, query arguments or card fields. APPLEPAY identifies the wallet;
the underlying network is NOT inferred. checkout_kind separates booking from
price-difference modifications. Amount is the actual SAR gateway amount on the
secure checkout/result (not a display-currency preference).

The UI callbacks do not submit/cancel/authorize payments, call executePayment,
alter validation or read provider form values. Telemetry exceptions are caught.
PCI fields, Apple tokens, identity, raw errors and provider IDs are excluded.
Per-event nullable payment metadata and ecommerce resets prevent inherited
fields from leaking into later unrelated GA4 events.

## GTM / GA4 contract

Container GTM-M9VLL75K, GA4 G-7E43GT39DN. Add nine named journey events to the
existing journey regex, without purchase/refund. Add five DLV variables:
payment_type, payment_brand, checkout_kind, payment_status, error_category.
Map those to Guest journey; map payment_type/brand to existing GA4 purchase.
Keep ecommerce Data Layer enabled. Keep Ads Purchase, purchase trigger, UPD,
Meta, consent and conversion count untouched.

Partial import is MERGE, overwrite only named conflicts. Expected change
summary: two GA4 tags modified, one journey trigger modified, five DLV added;
zero deletes. The unchanged purchase trigger is included only as a dependency.
Review the import preview before confirmation. Rollback: published GTM v12
plus revert this narrowly scoped website commit; never replay a purchase.

For user-facing GA4 reports register event-scoped custom dimensions for the
five safe fields, reusing existing definitions if present. These new fields
are not retroactive. Do not promote intermediate stages to Ads purchases.
Absence of a later event suggests drop-off but is not proof of payment failure.

## Verification

Node tests cover consent, startup ordering, programmatic fallback, errors,
callback isolation and payment metadata. Django tests cover verified purchase
brand, existing receipt identifiers, public checkout context and languages.
Existing purchase delivery, HyperPay, GA4 UPD, Ads UPD and Meta regressions
are included. No live charge, simulated advertising click or historical
purchase replay is performed by this change.

Sources:
- https://gate2play.docs.oppwa.com/integrations/widget/api
- https://developers.google.com/analytics/devguides/collection/ga4/reference/events#add_payment_info
