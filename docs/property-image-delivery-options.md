# Property image delivery options

Status: proposal only. No proxy, copy job, bucket, Worker, DNS route, CSP source, or
production flag is created by guest-audit batch 5.

## Current boundary

Hostaway remains the source of truth for original property images, their source
identifiers, order, and active state. The site may cache or copy those bytes for
delivery, but must continue to retain the Hostaway identity and must remove or
stop serving a copy when the corresponding source image becomes inactive.

The observed catalogue currently contains about 30 remote images. For the cost
examples below, the working range is 2–5 MB per original (roughly 60–150 MB in
total) and three delivery variants per image. Actual traffic and byte sizes must
be measured before purchase.

## Option A — Cloudflare transformation/proxy over the source URL

The browser requests a first-party image path. Cloudflare validates the requested
source against an exact host allowlist, fetches the Hostaway original on a cache
miss, transforms it, and caches the result at the edge.

Advantages:

- lowest migration effort and no duplicate-original lifecycle;
- first-party URLs, automatic modern formats, and responsive variants;
- 30 images × 3 variants is about 90 unique monthly transformations, comfortably
  within the current 5,000-transformation free allowance.

Risks and controls:

- a cache miss still depends on the external source, so this does not establish
  full availability control;
- accepting arbitrary source URLs would create an SSRF/open-proxy risk: only the
  exact approved Hostaway and legacy source hosts may be fetched, with normalized
  URLs, size/time limits, and no redirects to an unapproved host;
- source hotlinking/licensing terms need owner confirmation;
- a Worker route consumes Worker requests even when its own cache serves the
  response. The free plan currently allows 100,000 requests per day; Workers Paid
  has a USD 5 monthly minimum and includes 10 million requests per month.

Cloudflare currently includes 5,000 unique image transformations per month and
charges USD 0.50 per additional 1,000 on Images Paid. A transformed source and
variant pair counts once per calendar month, not once per view. See the official
[Cloudflare Images pricing](https://developers.cloudflare.com/images/pricing/),
[transformation flow](https://developers.cloudflare.com/images/optimization/transformations/overview/),
and [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/).

## Option B — copy originals to R2 during image synchronization

After Hostaway synchronization accepts image metadata, a separate idempotent job
downloads each approved original, verifies its content, and stores it under a key
derived from the Hostaway listing/image identifiers and a content hash. Public
pages use the stored copy only after the upload completes successfully; otherwise
they keep the source URL. Deactivation schedules a retention-aware deletion.

Advantages:

- removes runtime dependence on the external origin after a successful copy;
- gives the owner retention, cache, lifecycle, and access control;
- R2 has no Internet egress fee, and the estimated 60–150 MB plus current request
  volume should remain inside its 10 GB storage, 1 million Class A, and 10 million
  Class B monthly free allowances.

Risks and controls:

- more synchronization states, retry/reconciliation logic, orphan cleanup, and
  operational monitoring;
- a stale copy must never imply that an image still exists at the source;
- storage keys and logs must not contain guest data or secrets;
- source licensing, deletion retention, and incident rollback rules need owner
  approval;
- current R2 Standard prices above the free tier are USD 0.015/GB-month, USD 4.50
  per million Class A operations, and USD 0.36 per million Class B operations.

See the official [R2 pricing and free tier](https://developers.cloudflare.com/r2/pricing/).

## Option C — R2 originals plus Cloudflare image transformations

This combines Option B's controlled originals with responsive edge variants. At
the present inventory, the expected R2 storage/operation usage and approximately
90 unique variants are within the published free allowances. It has the strongest
delivery control, but also the largest implementation and reconciliation surface.
Cloudflare itself recommends R2 plus Images when custom storage controls and image
transformations are both needed; see the official
[Images introduction](https://developers.cloudflare.com/images/get-started/introduction/).

Cloudflare Images can instead store originals as a fully managed product. Its
current paid pricing is USD 5 per 100,000 stored images per month plus USD 1 per
100,000 delivered images. That is operationally simpler but introduces a minimum
storage increment and a more product-specific ingestion lifecycle.

## Recommendation and owner decision

Recommended after approval: Option C, rolled out in two guarded stages—copy and
reconcile originals first, then enable a small fixed set of variants. It controls
availability without changing Hostaway ownership of source metadata and avoids an
open proxy.

Before implementation, the owner must approve:

1. delivery option and monthly budget ceiling;
2. permission to copy and retain Hostaway/legacy source image bytes;
3. retention period after a source image is removed;
4. the exact source-host allowlist and whether the legacy Airbnb-hosted image may
   be copied;
5. an object-storage credential and custom delivery hostname provisioned outside
   Git.

No CSP wildcard or `unsafe-eval` is required by any recommended design.
