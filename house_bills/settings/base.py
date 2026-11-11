"""Shared configuration; installation values belong in the private environment."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATE_DIR = Path(os.environ.get("HOUSE_BILLS_STATE_DIR", "/state"))
DATA_DIR = Path(os.environ.get("HOUSE_BILLS_DATA_DIR", STATE_DIR / "development"))


def env_bool(name, default=False):
    value = os.environ.get(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ImproperlyConfigured(f"{name} must be true or false.")
    return value in {"true", "1"}


def env_list(name):
    return [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]


INSTALLED_APPS = [
    "house_bills.admin_site.HouseBillsAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "bills.apps.BillsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "bills.uploads.BoundedUploadMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "house_bills.urls"
WSGI_APPLICATION = "house_bills.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "house_bills" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "bills.context_processors.app_release",
            ],
        },
    }
]
DATABASES = {
    "default": {
        "ENGINE": "bills.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 10, "transaction_mode": "IMMEDIATE"},
        "ATOMIC_REQUESTS": False,
        "CONN_MAX_AGE": 0,
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "bills:invoices"
LOGOUT_REDIRECT_URL = "login"
LANGUAGE_CODE = "en"
TIME_ZONE = os.environ.get("HOUSE_BILLS_TIME_ZONE", "Europe/Stockholm")
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        "OPTIONS": {"file_permissions_mode": 0o644, "directory_permissions_mode": 0o755},
    },
}
# Originals are served only by authenticated views, never by static/media routing.
MEDIA_ROOT = DATA_DIR / "invoices"
MEDIA_URL = "/private-files/"
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
try:
    MAX_PDF_UPLOAD_BYTES = int(os.environ.get("HOUSE_BILLS_MAX_PDF_UPLOAD_BYTES", 20 * 1024 * 1024))
except ValueError as exc:
    raise ImproperlyConfigured("HOUSE_BILLS_MAX_PDF_UPLOAD_BYTES must be an integer.") from exc
if not 1 <= MAX_PDF_UPLOAD_BYTES <= 100 * 1024 * 1024:
    raise ImproperlyConfigured("HOUSE_BILLS_MAX_PDF_UPLOAD_BYTES must be between 1 and 104857600.")
MAX_UPLOAD_REQUEST_BYTES = MAX_PDF_UPLOAD_BYTES + 64 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 256 * 1024
# A full review has 20 allocations, 60 usages and 60 identifiers (over 1,100 fields).
# Include management/CSRF fields and URL-encoded Unicode values at their maximum lengths.
DATA_UPLOAD_MAX_MEMORY_SIZE = 512 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1500
DATA_UPLOAD_MAX_NUMBER_FILES = 1
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
MIGRATE_ON_START = env_bool("HOUSE_BILLS_MIGRATE_ON_START", True)
MIGRATION_LOCK_TIMEOUT = 300
APP_VERSION = os.environ.get("HOUSE_BILLS_VERSION", "development").strip() or "development"
UPDATE_CHECK_ENABLED = env_bool("HOUSE_BILLS_UPDATE_CHECK", True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
