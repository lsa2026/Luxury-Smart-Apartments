"""Guest account jobs carry opaque challenge/import identifiers, not contacts."""

from celery import shared_task

from .phone_auth import deliver_phone_code  # noqa: F401


@shared_task(
    name="apps.accounts.tasks.import_booking_history_page",
    soft_time_limit=25,
    time_limit=35,
    acks_late=True,
    reject_on_worker_lost=True,
)
def import_booking_history_page(job_id, generation):
    from .booking_history import run_page

    return {"status": run_page(job_id, generation)}
