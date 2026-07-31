"""Validate exact-path legacy redirects without changing data."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.urls import Resolver404, resolve

from apps.core.models import LegacyRedirect


class Command(BaseCommand):
    help = "Validate legacy redirect safety, loops, and local destinations."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--show-unmatched", action="store_true")
        parser.add_argument("--check-loops", action="store_true")
        parser.add_argument("--strict", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        del args
        redirects = list(LegacyRedirect.objects.filter(is_active=True))
        invalid = 0
        loops = 0
        missing = 0
        for redirect in redirects:
            try:
                redirect.full_clean()
            except ValidationError:
                invalid += 1
            if (
                options["check_loops"]
                and redirect.destination_path
                and LegacyRedirect.objects.filter(
                    source_path=redirect.destination_path,
                    destination_path=redirect.source_path,
                    is_active=True,
                ).exists()
            ):
                loops += 1
            if redirect.redirect_type != LegacyRedirect.RedirectType.GONE and not self._resolves(
                redirect.destination_path
            ):
                missing += 1

        self.stdout.write(
            "redirects={total} invalid={invalid} loops={loops} "
            "missing_destinations={missing} dry_run={dry_run}".format(
                total=len(redirects),
                invalid=invalid,
                loops=loops,
                missing=missing,
                dry_run=str(bool(options["dry_run"])).lower(),
            )
        )
        if options["show_unmatched"]:
            self.stdout.write(
                "unmatched_pattern=/listing/<legacy-slug>/ status=manual_mapping_required"
            )
        if options["strict"] and (invalid or loops or missing):
            raise CommandError("legacy_redirect_validation_failed")

    @staticmethod
    def _resolves(path: str) -> bool:
        try:
            match = resolve(path)
        except Resolver404:
            return False
        return not match.route.startswith(("admin/", "integrations/", "health/"))
