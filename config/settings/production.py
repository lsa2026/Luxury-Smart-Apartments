"""Hardened production settings."""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = False

# Render injects the assigned public hostname at runtime. Trusting it keeps the
# service reachable on its own domain without hardcoding a generated name, while
# any custom domain still has to be listed explicitly in DJANGO_ALLOWED_HOSTS.
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:  # noqa: F405
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)  # noqa: F405

if not ALLOWED_HOSTS:  # noqa: F405
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS is required in production.")

# Django validates the Origin header on unsafe requests behind TLS termination,
# so every allowed host needs a matching https origin.
CSRF_TRUSTED_ORIGINS = [
    f"https://*{host}" if host.startswith(".") else f"https://{host}"
    for host in ALLOWED_HOSTS  # noqa: F405
    if host != "*"
]

# Hashed, compressed static files served by WhiteNoise from the web process.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
HOSTAWAY_REQUIRE_SHARED_TOKEN_CACHE = True

if HOSTAWAY_AUTO_SYNC_ENABLED and not REDIS_URL:  # noqa: F405
    raise ImproperlyConfigured("REDIS_URL is required when automatic Hostaway sync is enabled.")
if HOSTAWAY_AUTO_SYNC_ENABLED and not CACHE_URL:  # noqa: F405
    raise ImproperlyConfigured("CACHE_URL is required when automatic Hostaway sync is enabled.")
if EMAIL_USE_TLS and EMAIL_USE_SSL:  # noqa: F405
    raise ImproperlyConfigured("EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be enabled.")
if EMAIL_DELIVERY_ENABLED:  # noqa: F405
    if EMAIL_BACKEND == "django.core.mail.backends.console.EmailBackend":  # noqa: F405
        raise ImproperlyConfigured(
            "The console email backend cannot be used for enabled production delivery."
        )
    required_email_settings = {
        "DEFAULT_FROM_EMAIL": DEFAULT_FROM_EMAIL,  # noqa: F405
        "EMAIL_BACKEND": EMAIL_BACKEND,  # noqa: F405
    }
    if EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend":  # noqa: F405
        required_email_settings["EMAIL_HOST"] = EMAIL_HOST  # noqa: F405
        required_email_settings["EMAIL_HOST_USER"] = EMAIL_HOST_USER  # noqa: F405
        required_email_settings["EMAIL_HOST_PASSWORD"] = EMAIL_HOST_PASSWORD  # noqa: F405
    missing = [name for name, value in required_email_settings.items() if not value]
    if missing:
        raise ImproperlyConfigured(
            f"Email delivery is enabled but required settings are missing: {', '.join(missing)}."
        )
if EMAIL_TASK_SCHEDULE_ENABLED and not REDIS_URL:  # noqa: F405
    raise ImproperlyConfigured("REDIS_URL is required when the email task schedule is enabled.")
