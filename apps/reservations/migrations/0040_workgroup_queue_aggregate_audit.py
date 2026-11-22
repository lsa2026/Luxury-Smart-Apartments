"""One-time internal aggregate audit; no domain rows or schema are modified."""

import json
from datetime import UTC, datetime

from django.db import migrations
from django.db.models import Case, CharField, Count, F, Value, When

PREFIX = "SITE01_WORKGROUP_QUEUE_AUDIT "
CONTRACT = "site01-workgroup-queue-audit-1"
STATES = (
    "selected",
    "pending",
    "queued",
    "planned",
    "prepared",
    "posting",
    "sending",
    "forwarding",
    "dispatching",
    "accepted",
    "sent",
    "persisted",
    "handed_off",
    "delivered",
    "read",
    "unknown",
    "uncertain",
    "blocked",
    "held",
    "failed",
    "satisfied",
    "superseded",
    "cancelled",
    "done",
    "queue_failed",
    "handoff_wait",
    "handoff_hold",
    "active",
    "expired",
    "completed",
)
# app, model, scenario field/fixed value, state field/fixed value, evidence meaning.
SOURCES = (
    ("reservations", "TelegramStayChannel", "scenario", None, None, "selected", "selected_channel"),
    ("reservations", "TelegramStayNotice", "scenario", None, "state", None, "source_notice"),
    (
        "reservations",
        "TelegramStayMessage",
        "scenario",
        None,
        "state",
        None,
        "retired_private_telegram",
    ),
    (
        "reservations",
        "TelegramWorkgroupHandoff",
        "scenario",
        None,
        "state",
        None,
        "workgroup_copy_only",
    ),
    (
        "reservations",
        "TelegramWebsiteHandoff",
        None,
        "website",
        "state",
        None,
        "workgroup_copy_only",
    ),
    (
        "reservations",
        "WebsiteMessageRelay",
        "scenario",
        None,
        "state",
        None,
        "previous_source_queue",
    ),
    (
        "reservations",
        "WebsiteMessageExpectation",
        "relay__scenario",
        None,
        "state",
        None,
        "central_receipt_evidence",
    ),
    (
        "reservations",
        "MorningCommunicationEvent",
        None,
        "07",
        "state",
        None,
        "legacy_source_persistence_only",
    ),
    (
        "reservations",
        "DayUseCommunicationEvent",
        None,
        "08",
        "state",
        None,
        "legacy_source_persistence_only",
    ),
    (
        "reservations",
        "Morning07PaymentEvent",
        None,
        "07",
        "state",
        None,
        "legacy_payment_source_queue",
    ),
    (
        "reservations",
        "DayUse08PaymentEvent",
        None,
        "08",
        "state",
        None,
        "legacy_payment_source_queue",
    ),
    (
        "reservations",
        "Morning07DeliveryAttempt",
        None,
        "07",
        "state",
        None,
        "legacy_private_guest_delivery",
    ),
    (
        "reservations",
        "DayUse08DeliveryAttempt",
        None,
        "08",
        "state",
        None,
        "legacy_private_guest_delivery",
    ),
    (
        "reservations",
        "MorningPaymentLinkDelivery",
        None,
        "07",
        "state",
        None,
        "legacy_payment_link_queue",
    ),
    (
        "reservations",
        "DayUsePaymentLinkDelivery",
        None,
        "08",
        "state",
        None,
        "legacy_payment_link_queue",
    ),
    (
        "reservations",
        "Morning07PaymentDelivery",
        None,
        "07",
        "state",
        None,
        "legacy_private_guest_delivery",
    ),
    (
        "reservations",
        "DayUse08PaymentDelivery",
        None,
        "08",
        "state",
        None,
        "legacy_private_guest_delivery",
    ),
    (
        "payments",
        "PaymentRequestDelivery",
        None,
        "website",
        "status",
        None,
        "previous_website_payment_queue",
    ),
)


def audit_queue_aggregates(apps, schema_editor):
    alias = schema_editor.connection.alias
    started = datetime.now(UTC).isoformat()
    for app, name, scenario_field, scenario, state_field, state, evidence in SOURCES:
        model = apps.get_model(app, name)
        scenario_value = (
            Case(
                When(**{scenario_field + "__in": ("07", "08")}, then=F(scenario_field)),
                default=Value("other"),
                output_field=CharField(),
            )
            if scenario_field
            else Value(scenario, output_field=CharField())
        )
        state_value = (
            Case(
                When(**{state_field + "__in": STATES}, then=F(state_field)),
                default=Value("other"),
                output_field=CharField(),
            )
            if state_field
            else Value(state, output_field=CharField())
        )
        # Only SQL-grouped, allowlisted labels and counts cross the DB boundary.
        # Clearing model ordering prevents timestamp/identifier grouping.
        rows = list(
            model.objects.using(alias)
            .order_by()
            .annotate(
                audit_scenario=scenario_value,
                audit_state=state_value,
            )
            .values("audit_scenario", "audit_state")
            .annotate(count=Count("*"))
            .order_by("audit_scenario", "audit_state")
        )
        result = {
            "contract": CONTRACT,
            "kind": "model_counts",
            "started_at": started,
            "model": app + "." + name,
            "evidence_kind": evidence,
            "counts": [
                {
                    "scenario": row["audit_scenario"],
                    "state": row["audit_state"],
                    "count": row["count"],
                }
                for row in rows
            ],
            "total": sum(row["count"] for row in rows),
        }
        print(PREFIX + json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
    # No completion marker is emitted if any source is unavailable. Missing is not zero.
    print(
        PREFIX
        + json.dumps(
            {
                "contract": CONTRACT,
                "kind": "complete",
                "models": len(SOURCES),
                "started_at": started,
                "completed_at": datetime.now(UTC).isoformat(),
                "consistency": "transaction_read_committed_aggregates",
                "domain_rows_modified": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0039_telegramwebsitehandoff"),
        ("payments", "0010_reservationpaymentrequest_paymentrequestdelivery_and_more"),
    ]
    operations = [migrations.RunPython(audit_queue_aggregates, migrations.RunPython.noop)]
