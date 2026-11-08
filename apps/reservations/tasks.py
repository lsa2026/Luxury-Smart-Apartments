from celery import shared_task


@shared_task(bind=True, ignore_result=True, max_retries=10)
def deliver_morning07_payment(self, event_id):
    from django.conf import settings

    from .models import Morning07PaymentEvent
    from .morning07_payments import deliver_event, enabled

    if not enabled() or not settings.MORNING07_PAYMENT_WEBHOOK_URL:
        return {"accepted": False}
    result = deliver_event(event_id)
    if Morning07PaymentEvent.objects.filter(pk=event_id, state="pending").exists():
        raise self.retry(countdown=30)
    return result


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
def complete_morning_payment_link(self, delivery_id, revision):
    from .models import MorningPaymentLinkDelivery
    from .morning_auto_payment import complete

    result = complete(delivery_id, revision)
    if result == "core_pending":
        if self.request.retries >= self.max_retries:
            MorningPaymentLinkDelivery.objects.filter(pk=delivery_id, state="waiting_core").update(
                state="expired", last_error="core_delivery_not_verified"
            )
            return "expired"
        raise self.retry(countdown=30)
    return result


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


@shared_task(bind=True, ignore_result=True, max_retries=10)
def deliver_day_use08_payment(self, event_id):
    from django.conf import settings

    from .day_use08_payments import deliver_event, enabled
    from .models import DayUse08PaymentEvent

    if not enabled() or not settings.DAY_USE08_PAYMENT_WEBHOOK_URL:
        return {"accepted": False}
    result = deliver_event(event_id)
    if DayUse08PaymentEvent.objects.filter(pk=event_id, state="pending").exists():
        raise self.retry(countdown=30)
    return result


@shared_task(bind=True, ignore_result=True, max_retries=5)
def deliver_day_use08_events(self, stay_id):
    from django.conf import settings

    from .day_use08_communications import deliver_pending_events
    from .models import DayUseCommunicationEvent

    if not settings.DAY_USE08_ENABLED:
        return {"accepted": 0}
    result = deliver_pending_events(stay_id=stay_id)
    if DayUseCommunicationEvent.objects.filter(stay_id=stay_id, state="pending").exists():
        raise self.retry(countdown=min(300, 30 * (self.request.retries + 1)))
    return result


@shared_task(ignore_result=True)
def deliver_day_use_payment_link(delivery_id):
    from .day_use_payment_messages import send_link

    return send_link(delivery_id)


@shared_task(bind=True, ignore_result=True, max_retries=10)
def complete_day_use_payment_link(self, delivery_id, revision):
    from .day_use_auto_payment import complete
    from .models import DayUsePaymentLinkDelivery

    result = complete(delivery_id, revision)
    if result == "core_pending":
        if self.request.retries >= self.max_retries:
            DayUsePaymentLinkDelivery.objects.filter(pk=delivery_id, state="waiting_core").update(
                state="expired", last_error="core_delivery_not_verified"
            )
            return "expired"
        raise self.retry(countdown=30)
    return result


@shared_task(bind=True, ignore_result=True, max_retries=8)
def sync_day_use_ownership(self, stay_id):
    from .day_use_ownership import sync

    if not sync(stay_id):
        raise self.retry(countdown=min(300, 30 * (self.request.retries + 1)))
    return True
