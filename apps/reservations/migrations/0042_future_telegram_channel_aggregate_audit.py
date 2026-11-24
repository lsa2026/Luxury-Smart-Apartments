"""One-time counts only; do not select guest fields or change channel ownership."""

import json

from django.conf import settings
from django.db import migrations
from django.db.models import Case, CharField, Count, Exists, OuterRef, Q, Value, When
from django.utils import timezone
from django.utils.dateparse import parse_datetime

PREFIX = "SITE01_FUTURE_TELEGRAM_CHANNEL_AUDIT "


def audit_future_channels(apps, schema_editor):
    observed = timezone.now()
    raw = getattr(settings, "MESSAGE_SOURCE_CUTOVER_AT", "")
    try:
        cutoff = parse_datetime(raw) if isinstance(raw, str) else None
    except ValueError:
        cutoff = None
    result = {
        "contract": "site01-future-telegram-channels-1",
        "observed_at": observed.isoformat(),
        "future_definition": "stay.starts_at>observed_at; all stay statuses",
        "attempt_definition": "recorded local notice or private/workgroup message attempt markers",
        "native_provider_nonattempted_verified": False,
        "domain_rows_modified": False,
        "eligibility_evaluated": False,
        "complete": False,
        "counts": None,
    }
    if cutoff is None or not timezone.is_aware(cutoff):
        result["reason"] = "source_cutover_unavailable"
    else:
        alias = schema_editor.connection.alias
        channel = apps.get_model("reservations", "TelegramStayChannel")
        notice = apps.get_model("reservations", "TelegramStayNotice")
        messages = [
            apps.get_model("reservations", name)
            for name in ("TelegramStayMessage", "TelegramWorkgroupHandoff")
        ]
        counts = []
        for scenario, name in (("07", "MorningStay"), ("08", "DayUseStay")):
            stay = apps.get_model("reservations", name)
            future = (
                stay.objects.using(alias).order_by().filter(starts_at__gt=observed).values("pk")
            )
            notices = (
                notice.objects.using(alias)
                .filter(scenario=scenario, stay_id=OuterRef("stay_id"))
                .filter(
                    Q(attempts__gt=0)
                    | Q(state__in=("posting", "unknown", "uncertain", "persisted"))
                )
            )
            attempted = Exists(notices)
            for model in messages:
                query = (
                    model.objects.using(alias)
                    .filter(scenario=scenario, stay_id=OuterRef("stay_id"))
                    .filter(
                        Q(attempt_id__isnull=False)
                        | Q(accepted_at__isnull=False)
                        | Q(
                            state__in=(
                                "posting",
                                "unknown",
                                "uncertain",
                                "accepted",
                                "handed_off",
                                "delivered",
                                "read",
                            )
                        )
                    )
                )
                attempted = attempted | Exists(query)
            rows = list(
                channel.objects.using(alias)
                .filter(scenario=scenario, selected_at__lt=cutoff, stay_id__in=future)
                .order_by()
                .annotate(
                    audit_attempt=Case(
                        When(attempted, then=Value("nonzero")),
                        default=Value("zero"),
                        output_field=CharField(),
                    )
                )
                .values("audit_attempt")
                .annotate(count=Count("*"))
                .order_by("audit_attempt")
            )
            buckets = {row["audit_attempt"]: row["count"] for row in rows}
            counts.append(
                {
                    "scenario": scenario,
                    "future_legacy_channels": sum(buckets.values()),
                    "zero_recorded_local_attempt_markers": buckets.get("zero", 0),
                    "nonzero_recorded_local_attempt_markers": buckets.get("nonzero", 0),
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
    dependencies = [("reservations", "0041_websitemessageexpectation_source_binding")]
    operations = [migrations.RunPython(audit_future_channels, migrations.RunPython.noop)]
