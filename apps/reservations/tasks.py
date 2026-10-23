from celery import shared_task


@shared_task(bind=True, ignore_result=True, max_retries=5)
def deliver_morning07_events(self, stay_id):
    from django.conf import settings

    from .models import MorningCommunicationEvent
    from .morning07_communications import deliver_pending_events

    if not settings.MORNING07_N8N_ENABLED:
        return {"accepted": 0}
    result = deliver_pending_events(stay_id=stay_id)
    if MorningCommunicationEvent.objects.filter(stay_id=stay_id, state="pending").exists():
        raise self.retry(countdown=min(300, 30 * (self.request.retries + 1)))
    return result
