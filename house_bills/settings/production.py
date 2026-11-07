"""Private HTTPS deployment; require explicit installation configuration."""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import DATA_DIR, env_bool, env_list

SECRET_KEY = os.environ.get("HOUSE_BILLS_SECRET_KEY", "")
ALLOWED_HOSTS = env_list("HOUSE_BILLS_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("HOUSE_BILLS_CSRF_TRUSTED_ORIGINS")
if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("REPLACE"):
    raise ImproperlyConfigured("Set a random HOUSE_BILLS_SECRET_KEY of at least 50 characters.")
if not ALLOWED_HOSTS or any("*" in host or "://" in host for host in ALLOWED_HOSTS):
    raise ImproperlyConfigured(
        "Set explicit HOUSE_BILLS_ALLOWED_HOSTS without schemes or wildcards."
    )
if not os.environ.get("HOUSE_BILLS_DATA_DIR") or not DATA_DIR.is_absolute():
    raise ImproperlyConfigured("Set HOUSE_BILLS_DATA_DIR to absolute, private host-local storage.")
if any(not origin.startswith("https://") or "*" in origin for origin in CSRF_TRUSTED_ORIGINS):
    raise ImproperlyConfigured("HOUSE_BILLS_CSRF_TRUSTED_ORIGINS must contain exact HTTPS origins.")
DEBUG = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
if env_bool("HOUSE_BILLS_TRUST_PROXY"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
