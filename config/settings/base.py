"""Shared Django settings."""

from decimal import Decimal
from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, []),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.core.apps.CoreConfig",
    "apps.accounts.apps.AccountsConfig",
    "apps.properties.apps.PropertiesConfig",
    "apps.reservations.apps.ReservationsConfig",
    "apps.payments.apps.PaymentsConfig",
    "apps.integrations.apps.IntegrationsConfig",
    "apps.reviews.apps.ReviewsConfig",
    "apps.notifications.apps.NotificationsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {"default": env.db("DATABASE_URL")}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "luxury-smart-apartments",
    }
}

REDIS_URL = env("REDIS_URL", default="").strip()
CACHE_URL = env("CACHE_URL", default="").strip()
if CACHE_URL:
    CACHES["default"] = {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": CACHE_URL,
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": ("django.contrib.auth.password_validation.UserAttributeSimilarityValidator")},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "ar"
LANGUAGES = [
    ("ar", "العربية"),
    ("en", "English"),
]
TIME_ZONE = "Asia/Riyadh"
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]
LANGUAGE_COOKIE_SAMESITE = "Lax"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"

HOSTAWAY_ACCOUNT_ID = env("HOSTAWAY_ACCOUNT_ID", default="")
HOSTAWAY_API_SECRET = env("HOSTAWAY_API_SECRET", default="")
HOSTAWAY_ACCESS_TOKEN = env("HOSTAWAY_ACCESS_TOKEN", default="")
HOSTAWAY_BASE_URL = env(
    "HOSTAWAY_BASE_URL",
    default="https://api.hostaway.com/v1",
)
HOSTAWAY_CONNECT_TIMEOUT = 5.0
HOSTAWAY_READ_TIMEOUT = 20.0
HOSTAWAY_MAX_GET_ATTEMPTS = 3
HOSTAWAY_CALENDAR_CACHE_TTL = 60
HOSTAWAY_PRICE_CACHE_TTL = 60
HOSTAWAY_TOKEN_CACHE_ALIAS = "default"
HOSTAWAY_TOKEN_CACHE_SAFETY_SECONDS = 300
HOSTAWAY_REQUIRE_SHARED_TOKEN_CACHE = False
HOSTAWAY_IMAGE_CSP_SOURCES = env.list(
    "HOSTAWAY_IMAGE_CSP_SOURCES",
    default=[
        "https://*.amazonaws.com",
        "https://*.hostaway.com",
        "https://a0.muscache.com",
    ],
)

PROPERTY_IMAGE_MAX_BYTES = 10 * 1024 * 1024
PROPERTY_IMAGE_MAX_WIDTH = 12_000
PROPERTY_IMAGE_MAX_HEIGHT = 12_000
PROPERTY_IMAGE_MAX_PIXELS = 50_000_000

AVAILABILITY_RATE_LIMIT_REQUESTS = 20
AVAILABILITY_RATE_LIMIT_WINDOW = 5 * 60


def optional_positive_int(name: str, default: int) -> int:
    """Read a positive integer while treating an empty environment value as unset."""
    raw_value = env(name, default="").strip()
    value = int(raw_value) if raw_value else default
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def strict_bool(name: str, default: bool = False) -> bool:
    """Parse an explicit environment boolean without truthy-string surprises."""
    raw_value = env(name, default="true" if default else "false").strip().casefold()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be an explicit boolean value.")


def optional_positive_int_or_none(name: str) -> int | None:
    raw_value = env(name, default="").strip()
    if not raw_value:
        return None
    value = int(raw_value)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer when configured.")
    return value


BOOKING_QUOTE_TTL_SECONDS = optional_positive_int("BOOKING_QUOTE_TTL_SECONDS", 600)
BOOKING_INTENT_TTL_SECONDS = optional_positive_int("BOOKING_INTENT_TTL_SECONDS", 1800)
BOOKING_INCOMPLETE_RETENTION_DAYS = optional_positive_int("BOOKING_INCOMPLETE_RETENTION_DAYS", 30)
BOOKING_PRICE_TOLERANCE = Decimal("0.00")
BOOKING_INTENT_RATE_LIMIT_REQUESTS = 5
BOOKING_INTENT_RATE_LIMIT_WINDOW = 10 * 60
BOOKING_READ_RATE_LIMIT_REQUESTS = 30
BOOKING_READ_RATE_LIMIT_WINDOW = 5 * 60

HOSTAWAY_LIVE_BOOKING_ENABLED = strict_bool("HOSTAWAY_LIVE_BOOKING_ENABLED")
HOSTAWAY_WEBHOOK_RECEIVER_ENABLED = strict_bool("HOSTAWAY_WEBHOOK_RECEIVER_ENABLED")
HOSTAWAY_WEBHOOK_PROCESSING_ENABLED = strict_bool("HOSTAWAY_WEBHOOK_PROCESSING_ENABLED")
HOSTAWAY_WEBHOOK_BASIC_AUTH_USERNAME = env(
    "HOSTAWAY_WEBHOOK_BASIC_AUTH_USERNAME",
    default="",
)
HOSTAWAY_WEBHOOK_BASIC_AUTH_PASSWORD = env(
    "HOSTAWAY_WEBHOOK_BASIC_AUTH_PASSWORD",
    default="",
)
HOSTAWAY_WEBHOOK_MAX_BODY_BYTES = optional_positive_int(
    "HOSTAWAY_WEBHOOK_MAX_BODY_BYTES",
    262_144,
)
HOSTAWAY_WEBHOOK_ALLOWED_EVENTS = tuple(
    value.strip()
    for value in env.list(
        "HOSTAWAY_WEBHOOK_ALLOWED_EVENTS",
        default=["reservation.created", "reservation.updated"],
    )
    if value.strip()
)
HOSTAWAY_RESERVATION_PROVIDER = env(
    "HOSTAWAY_RESERVATION_PROVIDER",
    default="LuxurySmartApartments",
).strip()
HOSTAWAY_DIRECT_CHANNEL_ID = optional_positive_int_or_none("HOSTAWAY_DIRECT_CHANNEL_ID")
HOSTAWAY_RESERVATION_REQUEST_TIMEOUT_SECONDS = optional_positive_int(
    "HOSTAWAY_RESERVATION_REQUEST_TIMEOUT_SECONDS",
    20,
)
HOSTAWAY_LIVE_MODIFICATION_ENABLED = strict_bool("HOSTAWAY_LIVE_MODIFICATION_ENABLED")
HOSTAWAY_LIVE_EXTENSION_ENABLED = strict_bool("HOSTAWAY_LIVE_EXTENSION_ENABLED")
HOSTAWAY_LIVE_CANCELLATION_ENABLED = strict_bool("HOSTAWAY_LIVE_CANCELLATION_ENABLED")
BOOKING_MODIFICATION_REQUEST_TTL_SECONDS = optional_positive_int(
    "BOOKING_MODIFICATION_REQUEST_TTL_SECONDS",
    1800,
)
BOOKING_EXTENSION_MAX_NIGHTS = optional_positive_int("BOOKING_EXTENSION_MAX_NIGHTS", 30)
BOOKING_MODIFICATION_CUTOFF_HOURS = optional_positive_int(
    "BOOKING_MODIFICATION_CUTOFF_HOURS",
    48,
)
BOOKING_CANCELLATION_REQUEST_ENABLED = strict_bool(
    "BOOKING_CANCELLATION_REQUEST_ENABLED",
    True,
)
BOOKING_AUTOMATIC_MODIFICATION_APPROVAL = strict_bool("BOOKING_AUTOMATIC_MODIFICATION_APPROVAL")
BOOKING_AUTOMATIC_CANCELLATION_ENABLED = strict_bool("BOOKING_AUTOMATIC_CANCELLATION_ENABLED")
BOOKING_MODIFICATION_RATE_LIMIT_REQUESTS = 10
BOOKING_MODIFICATION_RATE_LIMIT_WINDOW = 10 * 60
HOSTAWAY_WEBHOOK_RATE_LIMIT_REQUESTS = 120
HOSTAWAY_WEBHOOK_RATE_LIMIT_WINDOW = 60
HOSTAWAY_WEBHOOK_MAX_PROCESSING_ATTEMPTS = 5

HOSTAWAY_AUTO_SYNC_ENABLED = strict_bool("HOSTAWAY_AUTO_SYNC_ENABLED")
HOSTAWAY_AUTO_SYNC_INTERVAL_MINUTES = optional_positive_int(
    "HOSTAWAY_AUTO_SYNC_INTERVAL_MINUTES",
    5,
)
HOSTAWAY_REVIEW_SYNC_INTERVAL_MINUTES = optional_positive_int(
    "HOSTAWAY_REVIEW_SYNC_INTERVAL_MINUTES",
    15,
)
HOSTAWAY_WEBHOOK_PROCESS_INTERVAL_MINUTES = optional_positive_int(
    "HOSTAWAY_WEBHOOK_PROCESS_INTERVAL_MINUTES",
    1,
)
BOOKING_EXPIRATION_INTERVAL_MINUTES = optional_positive_int(
    "BOOKING_EXPIRATION_INTERVAL_MINUTES",
    5,
)
HOSTAWAY_AUTO_PUBLISH_NEW_LISTINGS = strict_bool(
    "HOSTAWAY_AUTO_PUBLISH_NEW_LISTINGS",
    True,
)
HOSTAWAY_AUTO_PUBLISH_REQUIRE_ACTIVE = strict_bool(
    "HOSTAWAY_AUTO_PUBLISH_REQUIRE_ACTIVE",
    True,
)
HOSTAWAY_AUTO_PUBLISH_REQUIRE_IMAGE = strict_bool(
    "HOSTAWAY_AUTO_PUBLISH_REQUIRE_IMAGE",
    True,
)
HOSTAWAY_AUTO_PUBLISH_REQUIRE_CAPACITY = strict_bool(
    "HOSTAWAY_AUTO_PUBLISH_REQUIRE_CAPACITY",
    True,
)
HOSTAWAY_AUTO_PUBLISH_REQUIRE_CURRENCY = strict_bool(
    "HOSTAWAY_AUTO_PUBLISH_REQUIRE_CURRENCY",
    True,
)
HOSTAWAY_AUTO_PUBLISH_REQUIRE_CITY = strict_bool(
    "HOSTAWAY_AUTO_PUBLISH_REQUIRE_CITY",
)
CELERY_SYNC_DISPATCH_ENABLED = strict_bool("CELERY_SYNC_DISPATCH_ENABLED")

CELERY_BROKER_URL = REDIS_URL or "redis://127.0.0.1:6379/0"
CELERY_RESULT_BACKEND = REDIS_URL or None
CELERY_TASK_IGNORE_RESULT = CELERY_RESULT_BACKEND is None
CELERY_TASK_TIME_LIMIT = 10 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 9 * 60
CELERY_BEAT_SCHEDULE = {}
if HOSTAWAY_AUTO_SYNC_ENABLED:
    CELERY_BEAT_SCHEDULE = {
        "hostaway-properties": {
            "task": "apps.integrations.tasks.sync_hostaway_properties_task",
            "schedule": HOSTAWAY_AUTO_SYNC_INTERVAL_MINUTES * 60,
        },
        "hostaway-reviews": {
            "task": "apps.integrations.tasks.sync_hostaway_reviews_task",
            "schedule": HOSTAWAY_REVIEW_SYNC_INTERVAL_MINUTES * 60,
        },
        "hostaway-webhooks": {
            "task": "apps.integrations.tasks.process_hostaway_webhooks_task",
            "schedule": HOSTAWAY_WEBHOOK_PROCESS_INTERVAL_MINUTES * 60,
        },
        "expire-booking-objects": {
            "task": "apps.integrations.tasks.expire_booking_objects_task",
            "schedule": BOOKING_EXPIRATION_INTERVAL_MINUTES * 60,
        },
    }

SITE_CANONICAL_URL = env("SITE_CANONICAL_URL", default="http://localhost:8000").rstrip("/")
CONTACT_RATE_LIMIT_REQUESTS = 5
CONTACT_RATE_LIMIT_WINDOW = 10 * 60

EMAIL_DELIVERY_ENABLED = strict_bool("EMAIL_DELIVERY_ENABLED")
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
).strip()
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="").strip()
SUPPORT_EMAIL = env("SUPPORT_EMAIL", default="").strip()
OPERATIONS_EMAIL = env("OPERATIONS_EMAIL", default="").strip()
EMAIL_HOST = env("EMAIL_HOST", default="").strip()
EMAIL_PORT = optional_positive_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="").strip()
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = strict_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = strict_bool("EMAIL_USE_SSL")
EMAIL_TIMEOUT = optional_positive_int("EMAIL_TIMEOUT_SECONDS", 15)
EMAIL_MAX_RETRIES = optional_positive_int("EMAIL_MAX_RETRIES", 3)
EMAIL_RETRY_DELAY_SECONDS = optional_positive_int("EMAIL_RETRY_DELAY_SECONDS", 60)
EMAIL_BRAND_NAME = env(
    "EMAIL_BRAND_NAME",
    default="Luxury Smart Apartments",
).strip()
SITE_BASE_URL = env("SITE_BASE_URL", default=SITE_CANONICAL_URL).rstrip("/")

NOTIFICATIONS_ENABLED = strict_bool("NOTIFICATIONS_ENABLED", True)
ADMIN_NOTIFICATION_EMAIL_ENABLED = strict_bool("ADMIN_NOTIFICATION_EMAIL_ENABLED")
CONTACT_NOTIFICATION_EMAIL_ENABLED = strict_bool("CONTACT_NOTIFICATION_EMAIL_ENABLED")
BOOKING_NOTIFICATION_EMAIL_ENABLED = strict_bool("BOOKING_NOTIFICATION_EMAIL_ENABLED")
MODIFICATION_NOTIFICATION_EMAIL_ENABLED = strict_bool(
    "MODIFICATION_NOTIFICATION_EMAIL_ENABLED",
)
EMAIL_TASK_SCHEDULE_ENABLED = strict_bool("EMAIL_TASK_SCHEDULE_ENABLED")

CONTACT_MESSAGE_RETENTION_DAYS = optional_positive_int(
    "CONTACT_MESSAGE_RETENTION_DAYS",
    365,
)
EMAIL_DELIVERY_RETENTION_DAYS = optional_positive_int(
    "EMAIL_DELIVERY_RETENTION_DAYS",
    180,
)
NOTIFICATION_RETENTION_DAYS = optional_positive_int(
    "NOTIFICATION_RETENTION_DAYS",
    180,
)
AUDIT_LOG_RETENTION_DAYS = optional_positive_int("AUDIT_LOG_RETENTION_DAYS", 730)

if EMAIL_TASK_SCHEDULE_ENABLED:
    CELERY_BEAT_SCHEDULE.update(
        {
            "process-email-queue": {
                "task": "apps.notifications.tasks.process_email_queue_task",
                "schedule": 60,
            },
            "cleanup-expired-notifications": {
                "task": "apps.notifications.tasks.cleanup_expired_notifications_task",
                "schedule": 24 * 60 * 60,
            },
            "daily-operations-summary": {
                "task": "apps.notifications.tasks.send_daily_operations_summary_task",
                "schedule": crontab(hour=8, minute=0),
            },
        }
    )

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": ("time={asctime} level={levelname} logger={name} message={message}"),
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "httpx": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "apps.integrations.hostaway": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
