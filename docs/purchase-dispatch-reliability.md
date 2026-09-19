# Purchase dispatch reliability

The result-page bridge contains a server-verified purchase with a stable opaque
transaction ID. Do not replay old paid/refunded bookings to test production tracking.

## Client state

- Queued in dataLayer: in-memory only. No receipt ACK or persistent success marker.
- Processed: the configured GTM container invoked eventCallback (no eventTimeout).
  Save a versioned session marker and POST the signed same-origin receipt ACK.
- Acknowledged: only after an HTTP 2xx response from our receipt endpoint.

A GTM processing callback is NOT proof of individual tag success, Google ingestion,
attribution, or appearance in Ads reports. The existing EMITTED server state denotes
browser dispatch, not Google receipt. Live Tag Assistant/network verification and
Ads reporting remain separate acceptance checks.

Failed GTM loads retry up to three times without duplicating the purchase or gtm.js
queue entries. A fresh result-page load can retry if GTM never processed the event.
ACK failures/timeouts retry separately, up to three attempts. An online event may
resume ACK attempts. A reload with a processed marker retries only the ACK.
The legacy pushed marker is ignored for a still-PREPARED server receipt; existing
EMITTED server records are not reopened or resent.

Consent behavior is unchanged: ordinary denied visits do not load GTM; a verified
purchase may load it with all denied states preserved. No guest identity is added
to the purchase payload. Consent-mode pings are not a guarantee of anonymity or
one-to-one advertising attribution.

## Verification

Run `node --test tests/analytics-purchase.test.cjs tests/purchase-delivery.test.cjs`
and the existing HyperPay/marketing Python suites. Use only mocked Google calls in
automated tests. After deployment verify the versioned JS on live landing pages,
then conduct an explicitly agreed real booking using Tag Assistant. Check value,
currency, transaction ID, consent states and duplicate protection. Do not fabricate
ad clicks or click the business's own ads to force attribution.
