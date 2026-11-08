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


@shared_task(ignore_result=True)
def deliver_morning_payment_link(delivery_id):
    from .morning_payment_messages import send_link

    return send_link(delivery_id)


@shared_task(bind=True, ignore_result=True, max_retries=10)
def invite_paid_morning(self, stay_id):
    """Bounded completion after payment; never replay the booking event or guest journey."""
    import json

    from django.conf import settings
    from django.utils import timezone

    from .models import MorningStay
    from .morning07_communications import verified_booking, verified_payment
    from .morning_loyalty import invite_paid_booking

    stay = MorningStay.objects.select_related("property").get(pk=stay_id)
    if (
        not settings.MORNING07_N8N_ENABLED
        or stay.environment != "production"
        or stay.status != "paid"
        or stay.ends_at <= timezone.now()
        or not verified_booking(stay)
        or not verified_payment(stay)
        or not stay.communication_revision
    ):
        return {"code": "not_eligible"}
    result = json.loads(invite_paid_booking(stay, stay.communication_revision).content)
    if result.get("code") in {"core_pending", "eligibility_unverified"}:
        raise self.retry(countdown=30)
    return result
