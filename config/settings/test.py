"""Isolated settings used only by the local automated test suite."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-production")
os.environ.setdefault("DJANGO_DEBUG", "False")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
# Automated tests must never inherit real provider credentials from a developer's .env.
os.environ["HYPERPAY_ENABLED"] = "false"
os.environ["HYPERPAY_ENTITY_ID"] = ""
os.environ["HYPERPAY_ACCESS_TOKEN"] = ""
os.environ["OPERATIONS_OWNER_ENFORCEMENT_ENABLED"] = "false"
os.environ["GOOGLE_SIGN_IN_ENABLED"] = "false"
os.environ["APPLE_SIGN_IN_ENABLED"] = "false"
# Tests exercise the operational event flow and must never inherit a local
# choice to mute it or to route mail through a real SMTP provider.
os.environ["NOTIFICATIONS_ENABLED"] = "true"
os.environ["EMAIL_LOCAL_SMTP_ENABLED"] = "false"

from .development import *  # noqa: E402,F403
