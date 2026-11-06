# Morning payment input contract — 6 November 2026

Scope: morning operations create/edit, saved billing, guest payment review/fallback,
and the last server-side gate before a morning checkout is created. No change to
ordinary reservations, payment links, the shared HyperPay payload builder, hosted
card collection, financial verification, messages, analytics or schedules.

References reviewed:
- HyperPay 3DS request guide: https://hyperpay.docs.oppwa.com/tutorials/threeDSecure/Parameters
  Published reference read in the review; later direct fetch returned 404. The
  published guide gives street <=50, city 2–45, email <=128, subdivision code
  <=3, ISO alpha-2 country. Its format differs from the general OPP API reference.
- HyperPay general API reference: https://hyperpay.docs.oppwa.com/reference/parameters
  Live direct retrieval currently returns 404; do not infer account-specific
  limits from another OPP provider's documentation.
- MPGS connector response observed in the owner's portal: postcodeZip over ten
  characters is rejected with INVALID_REQUEST / 800.100.156. No live test charge.
- MPGS public reference (retrieved successfully):
  https://ksa.gateway.mastercard.com/api/documentation/apiDocumentation/checkout/version/23/function/configure.html?locale=en_US
- Saudi postcode: https://business.splonline.com.sa/sign-up.html (five ASCII digits).
- Exact Saudi subdivision mappings, no guessed or truncated values:
  https://www.iso.org/files/live/sites/isoorg/files/archive/pdf/en/iso_3166-2_newsletter_ii-3_2011-12-13.pdf

Contract:
- Given name and remaining surname: nonempty with letters, each <=50. Full name
  is split at the first space, preserving multiword surnames and Arabic names.
- Email: valid email, 6–128. International phone: valid E.164 (max 15 digits).
  The morning-only payload sends the validated phone as +country-national for 3DS.
- Street 1–50; city 2–45; recognized country selected from the existing ISO list.
- Region: accept exact known Saudi province names/codes, map to the ISO suffix
  only at the payment boundary, preserving original input in the stay. Other
  countries require a 1–3 ASCII alphanumeric subdivision code (optional country
  prefix is removed). Format validation does not verify every foreign region's
  existence; Google Places is an aid, not authoritative address validation.
- Postcode: <=10 input characters, Latin letters/digits and internal spaces or
  hyphens; Saudi Arabia exactly five ASCII digits. Never fabricate a postcode for
  countries without one or silently truncate an address.
- Amount: existing approved morning limit 1–9,999,999 SAR, two decimal places;
  checked against server data. This is an application limit, not a claimed
  universal HyperPay merchant limit.

Valid saved addresses remain review-only and override guest POST tampering.
Invalid legacy addresses are prefilled for correction, with visible guidance;
corrected data is persisted only under the checkout claim lock after guest
consent and successful validation. Validation runs again inside the lock before
any payment attempt is inserted. Invalid fields cause no provider or availability
request and do not change the financial state. Legacy invalid guest identity is
blocked with a field-specific explanation and must be corrected by management.

Persistent bold guidance is inside each input's surrounding control, associated
with aria-describedby. Client-side errors are field-specific, announced politely,
and clear after correction. JavaScript is progressive enhancement: server checks
remain in effect with JavaScript disabled or a crafted POST.

Rollback: revert this change's commit and redeploy. No schema or environment
migration and no production data rewrite. Addresses legitimately corrected by a
guest are not automatically reverted. Provider/issuer approval is never guaranteed
by format validation; no live charge, reservation or guest communication is used
for verification.
