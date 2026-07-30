"""Shared Django settings."""

from pathlib import Path

import environ

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
