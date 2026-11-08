# Owner-authorized partial backup replacement — 2026-11-08

The owner explicitly requested deleting obsolete copies, creating a fresh copy
now, and moving daily refresh to 08:00 Asia/Riyadh. This concerns the existing
private loyalty mirror, not a full-site backup.

An optional worker-only LSA_LOYALTY_REPLACEMENT_REQUEST value queues one
idempotent owner replacement on worker readiness. The new source snapshot must
pass validation before the destination is changed. The existing manifest row is
locked, the project/format/source are checked, and the old mirror rows are
deleted in the same transaction as new insertion, manifest update, readback and
success status. Any write, verification or commit failure rolls the deletion back.

The request ID is retained in status across normal daily updates. Repeated
deliveries of that request do not replace again. Clear the environment request
after verifying success. No new endpoint, grant, table or scheduled definition
is created. The 01:00 backup schedule becomes 08:00; other schedules are unchanged.

Tests use synthetic snapshots and a transactional transport. Live completion
requires Render task success plus independent reads of technical manifest/status
metadata. A passed CI run is not proof of a live backup or a full recovery test.
