"""One-time metadata only: remaining legacy phases and a fixed timezone probe."""

import importlib.metadata
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import migrations
from django.db.models import Case, CharField, Count, Exists, OuterRef, Q, Value, When
from django.utils import timezone
from django.utils.dateparse import parse_datetime

PREFIX = "SITE01_ONGOING_TELEGRAM_CHANNEL_AUDIT "
PHASES = {
    "checkout": ("reminder_checkout", "checkout_reminder"),
    "reviews": (
        "review_request",
        "review_reminder_1",
        "review_reminder_2",
        "review_reminder_3",
        "review_followup_1",
        "review_followup_2",
        "review_followup_3",
    ),
}


def timezone_probe():
    try:
        version = importlib.metadata.version("tzdata")
    except importlib.metadata.PackageNotFoundError:
        version = None
    try:
        instant = datetime(2026, 11, 24, 12, tzinfo=UTC)
        local = instant.astimezone(ZoneInfo("Africa/Casablanca"))
        return {
            "zone": "Africa/Casablanca",
            "input_utc": instant.isoformat(),
            "local": local.isoformat(),
            "offset_minutes": int(local.utcoffset().total_seconds() / 60),
            "python_tzdata_package": version,
            "available": True,
        }
    except Exception:
        return {"zone": "Africa/Casablanca", "available": False, "python_tzdata_package": version}


def audit_ongoing_channels(apps, schema_editor):
    observed = timezone.now()
    raw = getattr(settings, "MESSAGE_SOURCE_CUTOVER_AT", "")
    try:
        cutoff = parse_datetime(raw) if isinstance(raw, str) else None
    except ValueError:
        cutoff = None
    result = {
        "contract": "site01-ongoing-telegram-channels-1",
        "observed_at": observed.isoformat(),
        "scope": "old selected channel; starts_at<=observed_at<ends_at; all stay statuses",
        "native_provider_nonattempted_verified": False,
        "domain_rows_modified": False,
        "eligibility_evaluated": False,
        "complete": False,
        "counts": None,
        "runtime_timezone_probe": timezone_probe(),
    }
    if cutoff is None or not timezone.is_aware(cutoff):
        result["reason"] = "source_cutover_unavailable"
    else:
        alias = schema_editor.connection.alias
        channels = apps.get_model("reservations", "TelegramStayChannel")
        notices = apps.get_model("reservations", "TelegramStayNotice")
        messages = [
            apps.get_model("reservations", n)
            for n in ("TelegramStayMessage", "TelegramWorkgroupHandoff")
        ]
        counts = []
        for scenario, name in (("07", "MorningStay"), ("08", "DayUseStay")):
            stays = (
                apps.get_model("reservations", name)
                .objects.using(alias)
                .order_by()
                .filter(starts_at__lte=observed, ends_at__gt=observed)
                .values("pk")
            )
            source_unknown = Exists(
                notices.objects.using(alias)
                .filter(scenario=scenario, stay_id=OuterRef("stay_id"))
                .filter(
                    Q(attempts__gt=0)
                    | Q(state__in=("posting", "unknown", "uncertain", "persisted"))
                )
            )
            for phase, purposes in PHASES.items():
                attempted = Q(pk__isnull=True)  # always false for a real selected channel
                uncertain = source_unknown
                for model in messages:
                    qs = model.objects.using(alias).filter(
                        scenario=scenario, stay_id=OuterRef("stay_id"), purpose__in=purposes
                    )
                    attempted = attempted | Exists(
                        qs.filter(
                            Q(attempt_id__isnull=False)
                            | Q(accepted_at__isnull=False)
                            | Q(state__in=("accepted", "sent", "handed_off", "delivered", "read"))
                        )
                    )
                    uncertain = uncertain | Exists(
                        qs.filter(~Q(state="planned") | ~Q(content_sha256=""))
                    )
                rows = list(
                    channels.objects.using(alias)
                    .order_by()
                    .filter(scenario=scenario, selected_at__lt=cutoff, stay_id__in=stays)
                    .annotate(
                        audit_bucket=Case(
                            When(attempted, then=Value("recorded_attempt")),
                            When(uncertain, then=Value("uncertain")),
                            default=Value("pristine_local"),
                            output_field=CharField(),
                        )
                    )
                    .values("audit_bucket")
                    .annotate(count=Count("*"))
                    .order_by("audit_bucket")
                )
                buckets = {r["audit_bucket"]: r["count"] for r in rows}
                counts.append(
                    {
                        "scenario": scenario,
                        "phase": phase,
                        "ongoing_legacy_channels": sum(buckets.values()),
                        **{
                            k: buckets.get(k, 0)
                            for k in ("pristine_local", "recorded_attempt", "uncertain")
                        },
                    }
                )
        result.update(
            complete=True,
            counts=counts,
            source_cutover=cutoff.isoformat(),
            consistency="read_committed_sql_aggregates",
        )
    print(PREFIX + json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)


class Migration(migrations.Migration):
    dependencies = [("reservations", "0042_future_telegram_channel_aggregate_audit")]
    operations = [migrations.RunPython(audit_ongoing_channels, migrations.RunPython.noop)]
