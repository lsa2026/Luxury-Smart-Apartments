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

from .development import *  # noqa: E402,F403
