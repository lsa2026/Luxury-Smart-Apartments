"""Reject production web deployments that would lock out the business owner."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check the production Google owner entrance without contacting Google."
    requires_system_checks = []

    def handle(self, *args, **options):
        required = {
            "GOOGLE_SIGN_IN_ENABLED": settings.GOOGLE_SIGN_IN_ENABLED,
            "GOOGLE_OAUTH_CLIENT_ID": settings.GOOGLE_OAUTH_CLIENT_ID,
            "GOOGLE_OAUTH_CLIENT_SECRET": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "SOCIALACCOUNT_LOGIN_ON_GET": settings.SOCIALACCOUNT_LOGIN_ON_GET,
            "OPERATIONS_OWNER_ENFORCEMENT_ENABLED": settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED,
        }
        missing = [name for name, value in required.items() if not value]
        app = settings.SOCIALACCOUNT_PROVIDERS.get("google", {}).get("APP", {})
        if not app.get("client_id") or not app.get("secret"):
            missing.append("SOCIALACCOUNT_PROVIDERS.google.APP")
        if missing:
            raise CommandError("Owner login is not ready: " + ", ".join(missing))
        self.stdout.write(self.style.SUCCESS("Google owner login configuration is ready."))
