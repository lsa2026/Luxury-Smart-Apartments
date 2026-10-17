# Clarity public-page installation — prepared, not deployed

## Scope and observed blocker

Owner requested installation via GTM on 2026-10-17. Existing project:
`yz1e54we6c`; current published GTM version: 14. Workspace 14 has one added,
unpublished Custom HTML tag (ID 52). No existing tag/trigger/variable was edited.
The original inline draft is blocked by production CSP. A fresh preview
document contained the compiled inline source, but no initializer state,
mask attribute or Clarity SDK element appeared. Production's live header allows
nonced inline scripts and does not include Clarity script/connect origins.
No live recording was verified. Existing project masking was corrected from
Relaxed to Strict and verified in its settings UI.

## Proposed changes requiring publication approval

* `static/js/clarity-public.js`: same-origin external initializer, invoked only
  from GTM. Not included automatically by base.html. Project ID is public.
* `apps/core/middleware.py`: add script origins `https://www.clarity.ms` and
  `https://scripts.clarity.ms`; connect origin `https://*.clarity.ms`; image
  origins `https://*.clarity.ms` and `https://c.bing.com`. Only successful GETs
  resolving to listed public views without query parameters. No changes to
  default-src, frame-src, form-action, unsafe-inline or unsafe-eval.
* `static/js/consent.js`: separate `clarityMeasurement` choice, false for legacy
  cookies. Requires analytics too. Fresh informed Accept all or explicit option
  can grant it; rejection clears it. Existing Google/Meta/UPD mapping and
  purchase loader remain unchanged. Versioned consent asset becomes 17.
* Consent descriptions and privacy/cookie notices identify Microsoft Clarity
  in all three site languages, describe masking, exclusions, withdrawal and
  technical data; no claim of complete anonymity or legal compliance.
* Synthetic boundary tests and CI step. No migration, environment setting,
  campaign setting or payment handler changes.

After website deployment, replace only tag 52's HTML with:

```html
<script src="https://luxurysmartapartments.com/static/js/clarity-public.js?v=1"></script>
```

Retain All Pages as the **bridge** trigger. The external SDK itself is gated
by explicit consent plus public URL and referral checks. The script prevents
repeat initialization and does not attach an identify or purchase event.
Do not publish GTM until the source asset exists and is verified.

## Privacy limits and verification still required

The first scope intentionally excludes all query strings/fragments, contact,
private reservations/accounts/admin/payment pages and sensitive referrals.
This excludes some campaign landing visits and is not full-journey coverage.
Analytics consent without the new recording choice does not start Clarity.
On withdrawal: ConsentV2 denied, then stop; a fresh page is needed to restart.
An in-flight request initiated before withdrawal can still finish. Runtime
behavior, stop semantics, cookie behavior and masked recording reception must
be verified after approval; local mocks are not delivery evidence.

Test one consenting public visit and one blocked query/account route, denied
and withdrawn consent, then verify masked recording inside the correct project.
No purchase, fake event, replay, guest data or new credentials are needed.
The existing purchase is not corrected/replayed by this work.

## Rollback

Pause only the new Clarity tag or republish retained GTM version 14. Revert only
this code change if needed; no schema or financial state rollback is involved.
Retain the original purchase tags. Pausing prevents future loads, not deletion
of records already received. There is no automatic monitoring or paid change.

## Official references reviewed

* https://learn.microsoft.com/en-us/clarity/third-party-integrations/google-tag-manager
* https://learn.microsoft.com/en-us/clarity/setup-and-installation/clarity-csp
* https://learn.microsoft.com/en-us/clarity/setup-and-installation/clarity-consent-api-v2
* https://github.com/microsoft/clarity/blob/master/packages/clarity-js/types/index.d.ts
