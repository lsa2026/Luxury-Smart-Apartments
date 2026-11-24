"""Future-only source selection and durable notification; never a guest sender."""

from datetime import timedelta
from uuid import uuid4

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import message_source_authorization as source
from .telegram_authority import Denied
from .telegram_stay_models import TelegramStayChannel, TelegramStayNotice

URLS = {
    "07": "https://lsa25.app.n8n.cloud/webhook/tg39-morning07-event",
    "08": "https://lsa25.app.n8n.cloud/webhook/tg39-dayuse08-event",
}


def cutover():
    try:
        value = parse_datetime(getattr(settings, "TELEGRAM_STAY_CUTOVER_AT", ""))
        return value if value and timezone.is_aware(value) else None
    except (TypeError, ValueError):
        return None


def enabled():
    at = cutover()
    return bool(
        getattr(settings, "TELEGRAM_STAY_MESSAGES_ENABLED", False) and at and at <= timezone.now()
    )


def scenario_for(stay):
    return next(
        (key for key, values in source.SOURCES.items() if isinstance(stay, values[0])), None
    )


def selected(stay):
    scenario = scenario_for(stay)
    return bool(
        scenario and TelegramStayChannel.objects.filter(scenario=scenario, stay_id=stay.pk).exists()
    )


def register(event, scenario, *, financial=False):
    if not enabled():
        return False
    stay = source.SOURCES[scenario][0].objects.get(pk=event.stay_id)
    at = cutover()
    if stay.environment != "production" or stay.created_at < at or event.created_at < at:
        return False
    if not selected(stay):
        from . import message_source_relay

        if (
            getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
            and message_source_relay.enabled()
            and message_source_relay.cutover() <= timezone.now()
        ):
            return False  # Existing channel ownership is never transferred or deleted.
    from . import day_use_hostaway, morning_hostaway

    owner = morning_hostaway if scenario == "07" else day_use_hostaway
    provider = owner.booking_state(stay).get("reservation_id")
    if not owner.hostaway_first(stay) or type(provider) is not int or provider <= 0:
        return False
    channel, _ = TelegramStayChannel.objects.get_or_create(
        scenario=scenario,
        stay_id=stay.pk,
        defaults={"hostaway_id": str(provider), "cutover_at": at},
    )
    if channel.hostaway_id != str(provider):
        return False  # Ownership remains fenced; never fall back to a legacy sender.
    row, created = TelegramStayNotice.objects.get_or_create(
        scenario=scenario,
        financial=financial,
        event_id=event.pk,
        defaults={"stay_id": stay.pk, "revision": stay.communication_revision},
    )
    if created:
        transaction.on_commit(lambda: queue(row.pk), robust=True)
    return True


def queue(notice_id, *, countdown=0):
    from .tasks import deliver_telegram_stay_notice

    deliver_telegram_stay_notice.apply_async(args=[notice_id], countdown=countdown)


def exhaustion_alert(row):
    """Use the existing idempotent operations mailbox, never a guest channel."""
    if row.attempts < 10 or row.state != "pending":
        return
    from apps.notifications.services.email import queue_email

    queue_email(
        message_type="integration_attention",
        recipient=settings.OPERATIONS_EMAIL,
        recipient_source="operations",
        recipient_reference=f"telegram-source:{row.scenario}:{row.pk}:{row.last_error}",
        language="ar",
        idempotency_key=f"telegram-source-exhausted:{row.pk}",
    )


def deliver(notice_id, *, client=None):
    if not enabled():
        return "paused"
    with transaction.atomic():
        row = TelegramStayNotice.objects.select_for_update().get(pk=notice_id)
        if row.state != "pending" or row.attempts >= 10:
            return row.state
        now = timezone.now()
        if row.lease_until and row.lease_until > now:
            return "leased"
        if row.next_attempt_at > now:
            return "waiting"
        # Commit the lease before notifying the durable n8n registry. The HTTP
        # operation is a persistence notification, not a provider message POST.
        row.attempts += 1
        row.lease_until = now + timedelta(seconds=45)
        row.next_attempt_at = now + timedelta(seconds=min(300, 15 * row.attempts))
        row.save(update_fields=["attempts", "lease_until", "next_attempt_at"])
    ok = False
    # Re-read the actual event before notification, without holding the notice
    # lock across a source-row lock or HTTP. Superseded events never become a
    # new timestamp or repeatedly trigger an old native job.
    from .telegram_stay_source import resolve

    try:
        with transaction.atomic():
            resolve(
                {
                    "contract": "tg39_stay_source_request_v1",
                    "scenario": row.scenario,
                    "operation": "snapshot",
                    "request_id": str(uuid4()),
                    "event_id": str(row.event_id),
                    "stay_key": ("morning07:" if row.scenario == "07" else "dayuse08:")
                    + str(row.stay_id),
                }
            )
    except (Denied, ValidationError, ValueError, TypeError, KeyError):
        TelegramStayNotice.objects.filter(pk=row.pk, state="pending").update(
            lease_until=None,
            last_error="source_not_yet_verified",
        )
        row.last_error = "source_not_yet_verified"
        exhaustion_alert(row)
        return "pending"
    try:
        envelope = {
            "contract": "tg39_stay_notification_v1",
            "scenario": row.scenario,
            "event_id": str(row.event_id),
        }
        if client is None:
            with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as remote:
                response = remote.post(URLS[row.scenario], json=envelope)
        else:
            response = client.post(URLS[row.scenario], json=envelope)
        body = response.json() if response.status_code == 200 else {}
        ok = (
            isinstance(body, dict)
            and set(body)
            == {
                "contract",
                "scenario",
                "event_id",
                "stay_key",
                "source_revision",
                "persisted",
                "channel",
            }
            and body["contract"] == "tg39_stay_event_ack_v1"
            and body["scenario"] == row.scenario
            and body["event_id"] == str(row.event_id)
            and body["stay_key"]
            == ("morning07:" if row.scenario == "07" else "dayuse08:") + str(row.stay_id)
            and type(body["source_revision"]) is int
            and body["source_revision"] == row.revision
            and body["persisted"] is True
            and body["channel"] == "telegram"
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        pass
    TelegramStayNotice.objects.filter(pk=row.pk, state="pending", attempts=row.attempts).update(
        state="persisted" if ok else "pending",
        lease_until=None,
        last_error="" if ok else "snapshot_persistence_unconfirmed",
    )
    if not ok:
        row.last_error = "snapshot_persistence_unconfirmed"
        exhaustion_alert(row)
    return "persisted" if ok else "pending"


def recover_due(limit=3):
    if not enabled():
        return 0
    now = timezone.now()
    # Recover the administrative alert after a crash between the tenth attempt
    # and queueing it. EmailDelivery's stable key prevents another email.
    for row in TelegramStayNotice.objects.filter(state="pending", attempts__gte=10).order_by("pk")[
        :limit
    ]:
        exhaustion_alert(row)
    identifiers = list(
        TelegramStayNotice.objects.filter(
            state="pending",
            attempts__lt=10,
            next_attempt_at__lte=now,
        )
        .exclude(lease_until__gt=now)
        .order_by("next_attempt_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    for identifier in identifiers:
        queue(identifier)
    return len(identifiers)
