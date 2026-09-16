"""Local development settings."""

from .base import *  # noqa: F403

DEBUG = env.bool("DJANGO_DEBUG", default=True)  # noqa: F405
ALLOWED_HOSTS = env.list(  # noqa: F405
    "DJANGO_ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1"],
)
# Local development never sends email unless the developer deliberately opts in.
# This makes an SMTP smoke test possible without making ordinary local browsing
# or test runs capable of contacting a real inbox.
EMAIL_LOCAL_SMTP_ENABLED = env.bool("EMAIL_LOCAL_SMTP_ENABLED", default=False)  # noqa: F405
if not EMAIL_LOCAL_SMTP_ENABLED:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
