"""Purge privacy-sensitive operational records using explicit retention rules."""

from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.models import ContactMessage
from apps.notifications.models import AuditLog, EmailDelivery, Notification


class Command(BaseCommand):
    help = "Purge expired operational data without touching reservations or payments."

    def add_arguments(self, parser: object) -> None:
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--model",
            choices=("all", "contacts", "email", "notifications", "audit"),
            default="all",
        )
        parser.add_argument("--before")
        parser.add_argument("--limit", type=int, default=1000)

    def handle(self, *args: object, **options: object) -> None:
        del args
        limit = options["limit"]
        if limit < 1 or limit > 10_000:
            raise CommandError("--limit must be between 1 and 10000.")
        requested_before = self._parse_before(options.get("before"))
        selected = options["model"]
        specs = {
            "contacts": (
                ContactMessage,
                "created_at",
                settings.CONTACT_MESSAGE_RETENTION_DAYS,
            ),
            "email": (
                EmailDelivery,
                "created_at",
                settings.EMAIL_DELIVERY_RETENTION_DAYS,
            ),
            "notifications": (
                Notification,
                "created_at",
                settings.NOTIFICATION_RETENTION_DAYS,
            ),
            "audit": (
                AuditLog,
                "created_at",
                settings.AUDIT_LOG_RETENTION_DAYS,
            ),
        }
        names = specs if selected == "all" else {selected: specs[selected]}
        result: dict[str, int] = {}
        for name, (model, date_field, retention_days) in names.items():
            retention_cutoff = timezone.now() - timedelta(days=retention_days)
            cutoff = (
                min(requested_before, retention_cutoff) if requested_before else retention_cutoff
            )
            ids = list(
                model.objects.filter(**{f"{date_field}__lt": cutoff})
                .order_by(date_field)
                .values_list("pk", flat=True)[:limit]
            )
            result[name] = len(ids)
            if ids and not options["dry_run"]:
                with transaction.atomic():
                    model.objects.filter(pk__in=ids).delete()
        mode = "DRY RUN" if options["dry_run"] else "PURGED"
        self.stdout.write(
            f"{mode}: " + ", ".join(f"{name}={count}" for name, count in sorted(result.items()))
        )

    @staticmethod
    def _parse_before(raw_value: str | None) -> datetime | None:
        if not raw_value:
            return None
        try:
            parsed_date = date.fromisoformat(raw_value)
        except ValueError as exc:
            raise CommandError("--before must use YYYY-MM-DD.") from exc
        return timezone.make_aware(datetime.combine(parsed_date, time.min))
