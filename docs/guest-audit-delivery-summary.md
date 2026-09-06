# Guest audit delivery summary

This document is the final handoff index for the five independent guest-audit
batches. Each batch targets `main`, remains owner-reviewed, and is not deployed or
merged by the implementation agent.

## Implemented

- [Batch 1 — booking blockers](https://github.com/xmansx2030-lgtm/Luxury-Smart-Apartments/pull/1):
  local Riyadh time rendering, Hostaway-derived indicative nightly anchors,
  gateway-minimum billing address, draft preservation, administrator-managed stay
  policies/defaults, and hero-image render hardening.
- [Batch 2 — Arabic content consistency](https://github.com/xmansx2030-lgtm/Luxury-Smart-Apartments/pull/2):
  manually curated amenity names, review cleanup and labels, aligned public rating
  sources, unified money formatting, and data-backed FAQ expansion.
- [Batch 3 — trust before payment](https://github.com/xmansx2030-lgtm/Luxury-Smart-Apartments/pull/4):
  authoritative price-detail presentation, provider-neutral guest copy,
  privacy-preserving approximate maps/nearby places, and branded missing-image
  handling with an administration warning.
- [Batch 4 — browsing and details](https://github.com/xmansx2030-lgtm/Luxury-Smart-Apartments/pull/5):
  date continuity without load-time availability calls, RTL-safe gallery counters,
  explicit visible-name/SEO contracts, homepage alt-text regression coverage, and
  an independent CI quality workflow.
- Batch 5 — polish and media readiness: balanced wide-screen quote layout, a full
  WhatsApp contact channel using the existing dashboard-managed contact source, a
  clear safety icon in place of the ambiguous decorative `LSA` badge, and six
  administrator-managed homepage image placements with required Arabic/English
  alt text and safe upload validation.

## Waiting for owner content or configuration

- cancellation-policy wording, house rules, and any site/property check-in or
  check-out defaults that differ intentionally from Hostaway;
- commercial FAQ answers for smoking, children, tax, identity requirements, and
  late arrival;
- approximate public map centres and every nearby-place claim;
- six licensed homepage replacement photographs and their Arabic, English, and
  optional French alt text;
- durable production media storage. The new homepage upload controls are ready,
  but the current Render filesystem is not a durable media store.

Until those values are approved, templates retain honest fallbacks rather than
inventing policy, location, price, or content.

## Proposed but intentionally not implemented

Property-image edge delivery is an architecture decision, not a bug patch.
[`property-image-delivery-options.md`](property-image-delivery-options.md) compares
a guarded Cloudflare proxy, synchronized R2 copies, and R2 plus Cloudflare image
transformations with current costs and risks. No option was enabled pending the
owner's budget, licensing, retention, and credential decisions.

No production deployment, PR merge, live payment/booking flag change, secret,
automatic translation, CSP wildcard, or locally derived final price is part of
these batches.
