"""Shared Django settings."""

from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import environ
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

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
    # Serves collected static assets directly from the web process on Render,
    # which has no separate static file server.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.core.middleware.LegacyRedirectMiddleware",
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
    ("fr", "Français"),
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

# The property admin renders every image as an inline row (~13 fields each), so a
# listing with 69 photos submits over 1,000 fields and Django rejects the save with
# TooManyFieldsSent. Raise the cap so image-heavy listings stay editable.
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000

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
# Read by availability browsing only; every binding step bypasses the cache.
HOSTAWAY_CALENDAR_CACHE_TTL = env.int("HOSTAWAY_CALENDAR_CACHE_TTL", default=60)
HOSTAWAY_PRICE_CACHE_TTL = env.int("HOSTAWAY_PRICE_CACHE_TTL", default=60)
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
if "https://images.unsplash.com" not in HOSTAWAY_IMAGE_CSP_SOURCES:
    HOSTAWAY_IMAGE_CSP_SOURCES.append("https://images.unsplash.com")

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
BOOKING_MANAGEMENT_ACCESS_RATE_LIMIT_REQUESTS = optional_positive_int(
    "BOOKING_MANAGEMENT_ACCESS_RATE_LIMIT_REQUESTS",
    5,
)
BOOKING_MANAGEMENT_ACCESS_RATE_LIMIT_WINDOW = optional_positive_int(
    "BOOKING_MANAGEMENT_ACCESS_RATE_LIMIT_WINDOW",
    15 * 60,
)
BOOKING_MANAGEMENT_SESSION_TTL_SECONDS = optional_positive_int(
    "BOOKING_MANAGEMENT_SESSION_TTL_SECONDS",
    4 * 60 * 60,
)

# Local, cardless payment simulation for development and acceptance testing only.
# DEBUG is deliberately part of the guard so this can never be enabled in production
# by a stale environment variable.
PAYMENT_SANDBOX_ENABLED = DEBUG and strict_bool("PAYMENT_SANDBOX_ENABLED")

# HyperPay TEST and production are separate, explicit configurations. Credentials
# are never shared between them and the base URL is pinned for each environment.
HYPERPAY_ENABLED = strict_bool("HYPERPAY_ENABLED")
HYPERPAY_ENVIRONMENT = env("HYPERPAY_ENVIRONMENT", default="test").strip().lower()
HYPERPAY_BASE_URL = env(
    "HYPERPAY_BASE_URL",
    default="https://eu-test.oppwa.com/",
).strip()
HYPERPAY_ENTITY_ID = env("HYPERPAY_ENTITY_ID", default="").strip()
HYPERPAY_ACCESS_TOKEN = env("HYPERPAY_ACCESS_TOKEN", default="").strip()
HYPERPAY_CURRENCY = env("HYPERPAY_CURRENCY", default="SAR").strip().upper()
HYPERPAY_PAYMENT_TYPE = env("HYPERPAY_PAYMENT_TYPE", default="DB").strip().upper()
HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED = strict_bool(
    "HYPERPAY_PREPAYMENT_REVALIDATION_ENABLED",
    True,
)
HYPERPAY_CONNECT_TIMEOUT = 5.0
HYPERPAY_READ_TIMEOUT = 20.0
HYPERPAY_ALLOWED_BRANDS = ("MADA", "VISA", "MASTER")
HYPERPAY_APPROVED_BASE_URLS = {
    "test": "https://eu-test.oppwa.com/",
    "production": "https://eu-prod.oppwa.com/",
}
HYPERPAY_WIDGET_ORIGIN = HYPERPAY_BASE_URL.rstrip("/")
if HYPERPAY_ENABLED:
    parsed_hyperpay_url = urlparse(HYPERPAY_BASE_URL)
    if (
        HYPERPAY_ENVIRONMENT not in HYPERPAY_APPROVED_BASE_URLS
        or HYPERPAY_BASE_URL
        != HYPERPAY_APPROVED_BASE_URLS.get(HYPERPAY_ENVIRONMENT)
        or parsed_hyperpay_url.scheme != "https"
        or HYPERPAY_CURRENCY != "SAR"
        or HYPERPAY_PAYMENT_TYPE != "DB"
    ):
        raise ImproperlyConfigured(
            "HyperPay requires an approved TEST or production host with SAR/DB."
        )
    if not HYPERPAY_ENTITY_ID or not HYPERPAY_ACCESS_TOKEN:
        raise ImproperlyConfigured(
            "HYPERPAY_ENTITY_ID and HYPERPAY_ACCESS_TOKEN are required when HyperPay is enabled."
        )

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
# Working days the guest is told a bank transfer takes. Shown wherever a
# refund is promised, so the promise is changed in one place.
BOOKING_REFUND_WORKING_DAYS = optional_positive_int("BOOKING_REFUND_WORKING_DAYS", 5)
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
        # Display-only anchor; daily is enough because the live quote decides
        # every real price. Runs before the working day in Riyadh.
        "indicative-rates": {
            "task": "apps.integrations.tasks.refresh_indicative_rates_task",
            "schedule": crontab(hour=5, minute=30),
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
EMAIL_LOGO_URL = env("EMAIL_LOGO_URL", default="").strip()
EMAIL_CONTACT_PHONE = env("EMAIL_CONTACT_PHONE", default="").strip()

# Fallback used by the floating WhatsApp button when SiteSetting has no number.
WHATSAPP_CONTACT_NUMBER = env("WHATSAPP_CONTACT_NUMBER", default="+966501205651").strip()
WHATSAPP_DEFAULT_COUNTRY_CODE = env("WHATSAPP_DEFAULT_COUNTRY_CODE", default="966").strip()

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

GOOGLE_INTEGRATIONS_ENABLED = strict_bool("GOOGLE_INTEGRATIONS_ENABLED")
GOOGLE_TAG_MANAGER_ENABLED = strict_bool("GOOGLE_TAG_MANAGER_ENABLED")
GOOGLE_TAG_MANAGER_CONTAINER_ID = env(
    "GOOGLE_TAG_MANAGER_CONTAINER_ID",
    default="",
).strip()
GOOGLE_ANALYTICS_ENABLED = strict_bool("GOOGLE_ANALYTICS_ENABLED")
GOOGLE_ANALYTICS_MEASUREMENT_ID = env(
    "GOOGLE_ANALYTICS_MEASUREMENT_ID",
    default="",
).strip()
GOOGLE_ANALYTICS_DEBUG_MODE = strict_bool("GOOGLE_ANALYTICS_DEBUG_MODE")
GOOGLE_ADS_ENABLED = strict_bool("GOOGLE_ADS_ENABLED")
GOOGLE_ADS_CONVERSION_ID = env("GOOGLE_ADS_CONVERSION_ID", default="").strip()
GOOGLE_ADS_BOOKING_CONVERSION_LABEL = env(
    "GOOGLE_ADS_BOOKING_CONVERSION_LABEL",
    default="",
).strip()
GOOGLE_ADS_CONTACT_CONVERSION_LABEL = env(
    "GOOGLE_ADS_CONTACT_CONVERSION_LABEL",
    default="",
).strip()
GOOGLE_ADS_ENHANCED_CONVERSIONS_ENABLED = strict_bool("GOOGLE_ADS_ENHANCED_CONVERSIONS_ENABLED")
GOOGLE_SITE_VERIFICATION = env("GOOGLE_SITE_VERIFICATION", default="").strip()
GOOGLE_SEARCH_CONSOLE_ENABLED = strict_bool("GOOGLE_SEARCH_CONSOLE_ENABLED")
GOOGLE_CONSENT_MODE_ENABLED = strict_bool("GOOGLE_CONSENT_MODE_ENABLED", True)
GOOGLE_CONSENT_DEFAULT_ANALYTICS_STORAGE = env(
    "GOOGLE_CONSENT_DEFAULT_ANALYTICS_STORAGE",
    default="denied",
).strip()
GOOGLE_CONSENT_DEFAULT_AD_STORAGE = env(
    "GOOGLE_CONSENT_DEFAULT_AD_STORAGE",
    default="denied",
).strip()
GOOGLE_CONSENT_DEFAULT_AD_USER_DATA = env(
    "GOOGLE_CONSENT_DEFAULT_AD_USER_DATA",
    default="denied",
).strip()
GOOGLE_CONSENT_DEFAULT_AD_PERSONALIZATION = env(
    "GOOGLE_CONSENT_DEFAULT_AD_PERSONALIZATION",
    default="denied",
).strip()
COOKIE_CONSENT_ENABLED = strict_bool("COOKIE_CONSENT_ENABLED", True)
COOKIE_CONSENT_VERSION = optional_positive_int("COOKIE_CONSENT_VERSION", 1)
COOKIE_CONSENT_MAX_AGE_DAYS = optional_positive_int(
    "COOKIE_CONSENT_MAX_AGE_DAYS",
    180,
)
ANALYTICS_EVENT_DEBUG_ENABLED = strict_bool("ANALYTICS_EVENT_DEBUG_ENABLED")
SITEMAP_CACHE_SECONDS = 300

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
