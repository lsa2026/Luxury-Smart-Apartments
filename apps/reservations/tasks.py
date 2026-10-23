from celery import shared_task


@shared_task(ignore_result=True)
def deliver_morning07_events():
    from .morning07_communications import deliver_pending_events

    return deliver_pending_events()
