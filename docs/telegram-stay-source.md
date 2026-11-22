# Dedicated stay Telegram source, incremental implementation

The fixed authenticated POST `/integrations/telegram39/stays/source/` resolves
the current MorningStay07/DayUseStay08 model and event; no caller phone/body,
URL, price or owner flag is used. The separate feature defaults off.

Snapshot is read-only and expires in30seconds. Hostaway ID is a string; revision
is a positive integer. Phone SHA256 uses canonical E164 digits without plus,
matching the existing Telegram binding convention. For08 the actual acknowledged
DayUseHostawayOwnership record is required. Ordinary/Gathern source exclusivity
must also be checked by registry39 before dispatch.

The current next operation returns wait/stay_dispatch_not_ready without a job.
This is deliberate partial composition, not proof of sender readiness. It does
not claim, post, record acceptance or release a payment/loyalty dependency.
Record-only recovery, dedicated activation proof, source render and Telegram
acceptance ledger are integrated separately against the shared39 contract.
Existing UltraMsg/YCloud transport and their historical delivery records remain
independent. No new booking, financial write or external message is performed by
this source. Synthetic tests exercise authentication, identity, ownership,
current window and absence of database writes; no live guest data is a fixture.
