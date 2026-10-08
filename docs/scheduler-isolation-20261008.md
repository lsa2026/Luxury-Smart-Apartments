# Dispatch-only scheduler recovery

Render's 2026-10-05 Blueprint sync enabled EMAIL_DELIVERY_ENABLED on lsa-beat.
The first observed same-day missing-password startup error was 20:59:35 Riyadh.
The last successful daily rate snapshot was 2026-10-05 16:30 Riyadh.
This establishes an invalid scheduler configuration, not a proven secret deletion.

## Change

Only lsa-beat switches to `celery -A scheduler_runtime.app:app beat`.
The scheduler imports neither Django settings nor business task modules, and
needs only the Redis broker and schedule flags. Email delivery credentials,
payments, Hostaway credentials and PostgreSQL are worker/web responsibilities.
The web and worker retain their existing production validation.

Both Django and the standalone scheduler use `scheduler_runtime.schedule`;
task names, queues, intervals and Riyadh timezone are unchanged. Price updates
remain daily at 16:30; Trustindex at 17:00; email processing and the 08:00
operations summary remain scheduled. No financial recovery retries are added.

## Deployment and verification

1. Run isolation, existing synchronization and price-calendar regression tests.
2. Merge the reviewed change; apply the production Blueprint so beat's command
   changes as well as its code. Do not add SMTP credentials to beat.
3. Confirm one beat instance starts, stays running and dispatches scheduled tasks
   received by the existing worker. Never start a second beat for testing.
4. Queue one price-calendar refresh through the existing authorized admin action.
   Confirm worker success and fresh public calendar data for all six listings.
5. Keep the wider twelve-service audit separate from this recovery.

Before rollback, check the command: reverting code alone while retaining the
new command would fail. The old Django-based beat command also requires the
old mail configuration to be corrected; do not silently restore that fault.
