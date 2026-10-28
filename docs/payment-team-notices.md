# Unified website payment notices

Owner approved a WhatsApp payment notice to the existing Marrakech work group
for every website HyperPay path: new booking, existing-reservation link,
reviewed amended link, modification checkout, and independent MorningStay.

The website uses its existing UltraMsg client and worker. The recipient is the
existing `MORNING07_WORK_GROUP_ID`, matched with `lsa07_settings.work_group_id`
during read-only inspection. No n8n workflow, guest journey, credential,
subscription or recurring schedule is created or modified.

Only a **new** successful gateway verification with a payment identifier,
verified timestamp and positive SAR amount queues the notice. A transactionally
persisted, unique per-capture outbox is dispatched after commit. Rechecking an
already successful payment does not create or backfill a notice. Existing unpaid
bookings and manual/offline collections do not trigger this financial receipt.

The Arabic message identifies the operation, guest, apartment, requested period,
actual SAR capture, source-currency order total, verification time and private
admin URL. It never includes card data, guest contact details or a bearer pay
link. A paid-but-unconfirmed/changed booking is explicitly marked for review.
Morning07's existing booking-status and guest messages remain independent;
this notification is specifically evidence of a new financial capture.

The send claim commits before the provider POST. An accepted provider response
is not proof that group members received/read the notice. An uncertain outcome
or crashed `sending` claim is never blindly reposted. The existing payment
recovery task recovers only queued notice rows, never historical captures.
Read-only audit evidence is available in the private PaymentTeamNotice admin.

Migration 0019 adds one independent table. It rewrites no financial history;
old application workers remain compatible. The feature defaults on only in
production, following the owner's authorization, and uses existing provider
configuration. `PAYMENT_TEAM_NOTICES_ENABLED=false` pauses this addition without
changing payment collection or Morning07. Retain the additive table on rollback.

Verification uses synthetic gateways and WhatsApp mocks, including rollback,
duplicate callbacks, timeout/uncertain outcomes, each checkout path and a
PostgreSQL worker-concurrency case. No historical booking is replayed and no
real guest or group message is used as a smoke test.
